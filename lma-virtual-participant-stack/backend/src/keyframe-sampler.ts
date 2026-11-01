/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */

/**
 * Key frames of the meeting display: the slides and documents that were on
 * screen, captured when they change and stored on the meeting.
 *
 * One low-rate FFmpeg process samples the VP's X11 display (the same display
 * video-recorder.ts records) once a second, scaled down to a 64x36 grayscale
 * grid and written to a pipe. keyframe-detector.ts decides from those samples
 * when the picture has changed and settled. For each key frame a single
 * full-resolution grab is taken and written as two JPEGs, a large one for
 * reading the slide and a thumbnail for the timeline, uploaded to the
 * recordings bucket under lma-video-keyframes/<CallId>/, and announced with an
 * ADD_VIDEO_KEYFRAME event.
 *
 * Sampling follows the meeting's recording rules: it runs only when video
 * recording is on for this VP (details.enableVideoRecording) and only while
 * the meeting is being transcribed (not before admission, not while paused
 * from chat). Where the platform shows when a screen share is on screen
 * (screen-share-probe.ts), samples are judged only while one is; elsewhere
 * every sample is judged, against stricter thresholds, so camera tiles do not
 * fill the timeline.
 *
 * Measured on the VP image limited to 2 vCPUs (see docs/meeting-video.md):
 * the sampling process uses about 0.6% of one core, against 7% for the
 * screen recording's encoder, and each key-frame grab about 125 ms of CPU.
 */
import { spawn, ChildProcess } from 'child_process';
import { existsSync, mkdirSync, readFileSync, unlinkSync } from 'fs';
import path from 'path';
import { randomBytes } from 'crypto';
import { S3Client, PutObjectCommand } from '@aws-sdk/client-s3';
import { KeyframeDetector, KeyframeThresholds } from './keyframe-detector.js';
import type { VideoKeyframeEvent } from './kinesis-stream.js';

const numberEnv = (name: string, fallback: number): number => {
    const raw = process.env[name];
    if (raw === undefined || raw === '') return fallback;
    const parsed = Number(raw);
    return Number.isFinite(parsed) && parsed >= 0 ? parsed : fallback;
};

export const GRID_WIDTH = numberEnv('KEYFRAME_GRID_WIDTH', 64);
export const GRID_HEIGHT = numberEnv('KEYFRAME_GRID_HEIGHT', 36);
export const SAMPLE_BYTES = GRID_WIDTH * GRID_HEIGHT;

/*
 * Defaults calibrated on a recorded synthetic meeting (docs/meeting-video.md):
 * consecutive slides built on one template differ in only 4-7% of the grid,
 * so the change fraction has to sit below that; a gallery of moving cameras
 * produced no frames at either setting, because the motion mask leaves the
 * tiles out.
 */

/** Thresholds while the platform shows a screen share. */
export const SHARE_THRESHOLDS: KeyframeThresholds = {
    changeFraction: numberEnv('KEYFRAME_CHANGE_FRACTION', 0.03),
    stableFraction: numberEnv('KEYFRAME_STABLE_FRACTION', 0.02),
    settleSamples: numberEnv('KEYFRAME_SETTLE_SAMPLES', 2),
    maxMovingFraction: numberEnv('KEYFRAME_MAX_MOVING_FRACTION', 0.5),
};

/**
 * Thresholds where the platform gives no screen-share signal: a larger part
 * of the picture has to change, and it has to hold still for longer, which a
 * talking head or a gallery of cameras rarely does.
 */
export const STRICT_THRESHOLDS: KeyframeThresholds = {
    changeFraction: numberEnv('KEYFRAME_STRICT_CHANGE_FRACTION', 0.04),
    stableFraction: numberEnv('KEYFRAME_STRICT_STABLE_FRACTION', 0.01),
    settleSamples: numberEnv('KEYFRAME_STRICT_SETTLE_SAMPLES', 3),
    maxMovingFraction: numberEnv('KEYFRAME_STRICT_MAX_MOVING_FRACTION', 0.25),
};

const SAMPLE_FPS = numberEnv('KEYFRAME_SAMPLE_FPS', 1);
const PIXEL_DELTA = numberEnv('KEYFRAME_PIXEL_DELTA', 12);
const MIN_INTERVAL_MS = numberEnv('KEYFRAME_MIN_INTERVAL_MS', 4000);
const MAX_PER_MEETING = numberEnv('KEYFRAME_MAX_PER_MEETING', 1000);
const SHARE_POLL_MS = numberEnv('KEYFRAME_SHARE_POLL_MS', 2000);
const IMAGE_WIDTH = numberEnv('KEYFRAME_IMAGE_WIDTH', 1568);
const THUMB_WIDTH = numberEnv('KEYFRAME_THUMB_WIDTH', 320);
const MAX_RESTARTS = 3;

const KEY_PREFIX = process.env.VIDEO_KEYFRAMES_KEY_PREFIX || 'lma-video-keyframes/';
const RESOLUTION = process.env.VIDEO_RESOLUTION || '1920x1080';
const DISPLAY = process.env.DISPLAY || ':99';
const REGION = process.env.AWS_REGION || 'us-east-1';
const WORK_DIR = '/tmp/video_keyframes';

/** Same rule as the Python posixify_filename that meeting deletion uses to find the prefix. */
export function posixifyFilename(name: string): string {
    return name.replace(/[^a-zA-Z0-9_.]/g, '_').replace(/^_+/, '').replace(/_+$/, '');
}

/** Zero-padded milliseconds, so ids sort in time order, plus a suffix that keeps them unique. */
export function frameIdFor(timestampSeconds: number, suffix: string = randomBytes(3).toString('hex')): string {
    const ms = Math.max(0, Math.round(timestampSeconds * 1000));
    return `${String(ms).padStart(10, '0')}-${suffix}`;
}

/** The S3 keys of a frame's two images. */
export function keyframeKeys(callId: string, frameId: string): { image: string; thumbnail: string } {
    const base = `${KEY_PREFIX}${posixifyFilename(callId)}/${frameId}`;
    return { image: `${base}.jpg`, thumbnail: `${base}_thumb.jpg` };
}

export interface KeyframeSamplerOptions {
    callId: string;
    bucket: string;
    /** Whether the meeting is being transcribed (not before admission, not paused). */
    isLive: () => boolean;
    /** Position on the transcript timeline, in seconds. */
    timelineSeconds: () => number;
    /** Wall-clock start of the video recording, or null when there is none. */
    recordingStartedAtMs: () => number | null;
    /** Whether a screen share is on screen; absent where the platform gives no signal. */
    shareActive?: () => Promise<boolean | null>;
    /** Announces a key frame (ADD_VIDEO_KEYFRAME). */
    emit: (frame: VideoKeyframeEvent) => Promise<void>;
}

/** The parts of the sampler that touch the outside world, replaceable in tests. */
export interface KeyframeSamplerIo {
    now: () => number;
    /** Grabs the display once, returning the large image and the thumbnail. */
    grab: () => Promise<{ image: Buffer; thumbnail: Buffer; width: number; height: number }>;
    upload: (bucket: string, key: string, body: Buffer) => Promise<void>;
}

const runFfmpeg = (args: string[]): Promise<void> =>
    new Promise((resolve, reject) => {
        const proc = spawn('ffmpeg', args, { stdio: ['ignore', 'ignore', 'pipe'] });
        let stderr = '';
        proc.stderr?.on('data', (d: Buffer) => {
            stderr = (stderr + d.toString()).slice(-2000);
        });
        const timer = setTimeout(() => proc.kill('SIGKILL'), 15_000);
        proc.on('error', (err) => {
            clearTimeout(timer);
            reject(err);
        });
        proc.on('exit', (code) => {
            clearTimeout(timer);
            if (code === 0) resolve();
            else reject(new Error(`ffmpeg exited with code ${code}: ${stderr.trim()}`));
        });
    });

const s3 = new S3Client({ region: REGION });

export const defaultIo: KeyframeSamplerIo = {
    now: () => Date.now(),
    grab: async () => {
        if (!existsSync(WORK_DIR)) mkdirSync(WORK_DIR, { recursive: true });
        const stem = path.join(WORK_DIR, `grab_${process.pid}_${Date.now()}`);
        const imagePath = `${stem}.jpg`;
        const thumbPath = `${stem}_thumb.jpg`;
        try {
            // One grab, two sizes: both images are the same instant.
            await runFfmpeg([
                '-nostdin', '-loglevel', 'error', '-y',
                '-f', 'x11grab', '-draw_mouse', '0', '-video_size', RESOLUTION, '-i', DISPLAY,
                '-filter_complex', `[0:v]split=2[a][b];[a]scale=${IMAGE_WIDTH}:-2[big];[b]scale=${THUMB_WIDTH}:-2[small]`,
                // -frames:v is per output: given once, the second output keeps
                // reading the display and fails on its second frame.
                '-map', '[big]', '-frames:v', '1', '-q:v', '4', imagePath,
                '-map', '[small]', '-frames:v', '1', '-q:v', '6', thumbPath,
            ]);
            const [w, h] = RESOLUTION.split('x').map(Number);
            const width = Math.min(IMAGE_WIDTH, w || IMAGE_WIDTH);
            const height = w && h ? Math.round((width * h) / w / 2) * 2 : 0;
            return { image: readFileSync(imagePath), thumbnail: readFileSync(thumbPath), width, height };
        } finally {
            for (const p of [imagePath, thumbPath]) {
                try {
                    if (existsSync(p)) unlinkSync(p);
                } catch {
                    /* temp file */
                }
            }
        }
    },
    upload: async (bucket, key, body) => {
        await s3.send(new PutObjectCommand({ Bucket: bucket, Key: key, Body: body, ContentType: 'image/jpeg' }));
    },
};

export class KeyframeSampler {
    private readonly io: KeyframeSamplerIo;
    private options: KeyframeSamplerOptions | null = null;
    private ffmpeg: ChildProcess | null = null;
    private sharePoll: ReturnType<typeof setInterval> | null = null;
    private detector = new KeyframeDetector({ sampleBytes: SAMPLE_BYTES, pixelDelta: PIXEL_DELTA, minIntervalMs: MIN_INTERVAL_MS });
    private pending: Buffer = Buffer.alloc(0);
    private shareOnScreen: boolean | null = null;
    private sampling = false;
    private capturing: Promise<void> | null = null;
    private stopped = true;
    private restarts = 0;
    private count = 0;
    private skippedWhileCapturing = 0;

    constructor(io: KeyframeSamplerIo = defaultIo) {
        this.io = io;
    }

    /** Key frames announced so far in this meeting. */
    get framesEmitted(): number {
        return this.count;
    }

    start(options: KeyframeSamplerOptions): void {
        if (!this.stopped) return;
        this.options = options;
        this.stopped = false;
        this.count = 0;
        this.restarts = 0;
        const mode = options.shareActive ? 'while a screen share is on screen' : 'all the time, with the stricter thresholds';
        console.log(`[Keyframes] Sampling ${DISPLAY} at ${SAMPLE_FPS}/s ${mode}`);
        if (options.shareActive) {
            const poll = async () => {
                const active = await options.shareActive!().catch(() => null);
                if (active !== null && active !== this.shareOnScreen) {
                    console.log(`[Keyframes] Screen share ${active ? 'on screen' : 'no longer on screen'}`);
                    this.shareOnScreen = active;
                }
            };
            void poll();
            this.sharePoll = setInterval(() => void poll(), SHARE_POLL_MS);
        }
        this.spawnSampler();
    }

    /** Stops sampling and waits for a key frame that is being captured. */
    async stop(): Promise<void> {
        if (this.stopped) return;
        this.stopped = true;
        if (this.sharePoll) clearInterval(this.sharePoll);
        this.sharePoll = null;
        this.ffmpeg?.kill('SIGTERM');
        this.ffmpeg = null;
        if (this.capturing) await this.capturing.catch(() => undefined);
        console.log(
            `[Keyframes] Stopped after ${this.count} key frame(s)` +
                (this.skippedWhileCapturing ? `; ${this.skippedWhileCapturing} sample(s) skipped during captures` : ''),
        );
    }

    private spawnSampler(): void {
        const args = [
            '-nostdin', '-loglevel', 'error',
            '-f', 'x11grab', '-draw_mouse', '0', '-framerate', String(SAMPLE_FPS), '-video_size', RESOLUTION, '-i', DISPLAY,
            '-vf', `scale=${GRID_WIDTH}:${GRID_HEIGHT}:flags=area,format=gray`,
            '-f', 'rawvideo', 'pipe:1',
        ];
        const proc = spawn('ffmpeg', args, { stdio: ['ignore', 'pipe', 'pipe'] });
        this.ffmpeg = proc;
        this.pending = Buffer.alloc(0);
        proc.stdout?.on('data', (chunk: Buffer) => this.onData(chunk));
        proc.stderr?.on('data', (d: Buffer) => console.log('[Keyframes] FFmpeg:', d.toString().trim()));
        proc.on('error', (err) => console.error(`[Keyframes] FFmpeg error: ${err.message}`));
        proc.on('exit', (code, signal) => {
            if (this.ffmpeg !== proc) return;
            this.ffmpeg = null;
            if (this.stopped) return;
            if (this.restarts >= MAX_RESTARTS) {
                console.error(`[Keyframes] Sampler exited (code=${code}, signal=${signal}); giving up after ${MAX_RESTARTS} restarts`);
                return;
            }
            this.restarts++;
            console.warn(`[Keyframes] Sampler exited (code=${code}, signal=${signal}); restarting (${this.restarts}/${MAX_RESTARTS})`);
            setTimeout(() => {
                if (!this.stopped) this.spawnSampler();
            }, 2000 * this.restarts);
        });
    }

    private onData(chunk: Buffer): void {
        this.pending = this.pending.length ? Buffer.concat([this.pending, chunk]) : chunk;
        while (this.pending.length >= SAMPLE_BYTES) {
            const sample = this.pending.subarray(0, SAMPLE_BYTES);
            this.pending = this.pending.subarray(SAMPLE_BYTES);
            this.handleSample(sample);
        }
    }

    /** Judges one sample; public so tests can drive the sampler without FFmpeg. */
    handleSample(sample: Uint8Array): void {
        const options = this.options;
        if (!options || this.stopped) return;
        const judged = options.isLive() && (!options.shareActive || this.shareOnScreen === true);
        if (!judged) {
            if (this.sampling) this.detector.pause();
            this.sampling = false;
            return;
        }
        this.sampling = true;
        if (this.count >= MAX_PER_MEETING) return;
        const nowMs = this.io.now();
        const thresholds = options.shareActive ? SHARE_THRESHOLDS : STRICT_THRESHOLDS;
        const decision = this.detector.push(sample, nowMs, thresholds);
        if (!decision.keep) return;
        if (this.capturing) {
            // One capture at a time. The detector has already taken this
            // picture as the reference, so a skipped one is not retried; with a
            // minimum interval of seconds and a capture well under one, this is
            // a guard rather than a path that runs.
            this.skippedWhileCapturing++;
            return;
        }
        // The frame is stamped with when its picture first appeared, on both clocks.
        const sinceSeen = Math.max(0, nowMs - decision.firstSeenMs) / 1000;
        const timestamp = Math.max(0, options.timelineSeconds() - sinceSeen);
        const recordingStart = options.recordingStartedAtMs();
        const videoOffset = recordingStart === null ? null : Math.max(0, (decision.firstSeenMs - recordingStart) / 1000);
        const shareDetected = options.shareActive ? true : null;
        this.capturing = this.capture(options, timestamp, videoOffset, shareDetected, decision.changeFraction)
            .catch((err) => console.error(`[Keyframes] Key frame not saved: ${err?.message || err}`))
            .finally(() => {
                this.capturing = null;
            });
    }

    private async capture(
        options: KeyframeSamplerOptions,
        timestamp: number,
        videoOffset: number | null,
        shareDetected: boolean | null,
        changeScore: number,
    ): Promise<void> {
        const grabbed = await this.io.grab();
        const frameId = frameIdFor(timestamp);
        const keys = keyframeKeys(options.callId, frameId);
        await Promise.all([
            this.io.upload(options.bucket, keys.image, grabbed.image),
            this.io.upload(options.bucket, keys.thumbnail, grabbed.thumbnail),
        ]);
        const url = (key: string) => `https://${options.bucket}.s3.${REGION}.amazonaws.com/${encodeURIComponent(key)}`;
        await options.emit({
            FrameId: frameId,
            Timestamp: Math.round(timestamp * 1000) / 1000,
            VideoOffset: videoOffset === null ? null : Math.round(videoOffset * 1000) / 1000,
            ImageUrl: url(keys.image),
            ThumbnailUrl: url(keys.thumbnail),
            Width: grabbed.width,
            Height: grabbed.height,
            Source: 'VIRTUAL_PARTICIPANT',
            ShareDetected: shareDetected,
            ChangeScore: Math.round(changeScore * 1000) / 1000,
        });
        this.count++;
        console.log(
            `[Keyframes] Key frame ${frameId} at ${timestamp.toFixed(1)}s ` +
                `(change ${(changeScore * 100).toFixed(0)}%, ${(grabbed.image.length / 1024).toFixed(0)}KB + ` +
                `${(grabbed.thumbnail.length / 1024).toFixed(0)}KB)`,
        );
    }
}

export const keyframeSampler = new KeyframeSampler();
