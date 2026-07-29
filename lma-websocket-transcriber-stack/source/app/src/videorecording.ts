/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 *
 * Optional desktop-video recording lane for the websocket transcriber.
 *
 * Wire protocol (fully backward compatible — servers that predate this module
 * silently ignore the unknown callEvent values, and clients that never send
 * them exercise none of this code):
 *
 *   - The client opens a SECOND websocket to the same /api/v1/ws endpoint,
 *     dedicated to video, so video bytes never delay real-time audio PCM.
 *   - Text frame {callEvent: 'START_VIDEO', callId, videoTimeOffsetMs?}
 *     announces the stream; every subsequent binary frame on that socket is a
 *     chunk of a fragmented-MP4 (H.264, video-only) stream.
 *   - Text frame {callEvent: 'END_VIDEO', callId} (or socket close) ends it.
 *
 * Sessions are correlated with the audio call by callId. At end of call the
 * video segments are muxed with the audio WAV (produced by the existing audio
 * recording path) into a single faststart MP4 via ffmpeg, uploaded to the
 * recordings bucket under VIDEO_RECORDINGS_KEY_PREFIX, and announced on KDS
 * with the same ADD_S3_VIDEO_RECORDING_URL event the Virtual Participant
 * emits — so the existing call_event_processor -> updateVideoRecordingUrl ->
 * UI VideoPlayer pipeline works unchanged.
 *
 * Ordering: audio END and video END arrive in either order (separate sockets).
 * Whichever side finishes last triggers the mux; a safety timer muxes anyway
 * if the other side never reports in.
 */
import fs from 'fs';
import path from 'path';
import { spawn } from 'child_process';
import { FastifyInstance } from 'fastify';
import { S3Client, PutObjectCommand } from '@aws-sdk/client-s3';

// Import from concrete modules (not the ./utils / ./calleventdata barrels):
// the barrels pull in jwt-verifier, which constructs a Cognito verifier at
// import time and hard-fails without USERPOOL_ID — breaking offline tests.
import { CallMetaData } from './calleventdata/eventtypes';
import { writeCallVideoRecordingEvent } from './calleventdata/transcribe';
import { posixifyFilename, normalizeErrorForLogging } from './utils/common';

const AWS_REGION = process.env['AWS_REGION'] || 'us-east-1';
const RECORDINGS_BUCKET_NAME =
    process.env['RECORDINGS_BUCKET_NAME'] || undefined;
const VIDEO_RECORDINGS_KEY_PREFIX =
    process.env['VIDEO_RECORDINGS_KEY_PREFIX'] || 'lma-video-recordings/';
const LOCAL_TEMP_DIR = process.env['LOCAL_TEMP_DIR'] || '/tmp/';
const ENABLE_VIDEO_RECORDING =
    (process.env['ENABLE_VIDEO_RECORDING'] || 'true') === 'true';
const FFMPEG_PATH = process.env['FFMPEG_PATH'] || 'ffmpeg';

/**
 * Read a numeric env var, falling back to `fallback` for anything that isn't a
 * finite number. Plain `parseInt` returns NaN for values like "2G" or "", and
 * NaN silently DEFEATS every comparison it appears in — e.g. a NaN size cap
 * makes `size > cap` always false, disabling the cap entirely, and a NaN
 * timeout makes setTimeout fire immediately.
 */
const numericEnv = (name: string, fallback: number): number => {
    const raw = process.env[name];
    if (raw === undefined || raw === '') {
        return fallback;
    }
    const parsed = Number(raw);
    if (!Number.isFinite(parsed) || parsed < 0) {
        // Module-scope: no server logger yet. stderr is captured by the task's
        // awslogs driver, so this is still visible in CloudWatch.
        process.stderr.write(
            `[VIDEO]: ignoring invalid ${name}="${raw}"; using ${fallback}\n`
        );
        return fallback;
    }
    return parsed;
};

// Cap the on-disk video size PER CALL. ~1 Mbps screen content is ~450 MB/hour.
const VIDEO_MAX_FILE_SIZE_BYTES = numericEnv(
    'VIDEO_MAX_FILE_SIZE_BYTES',
    2 * 1024 * 1024 * 1024
);
// Cap the on-disk video size across ALL concurrent calls. The per-call cap
// alone is not enough: this single Fargate task serves many calls sharing one
// ephemeral volume (20 GiB by default), and that volume also holds every
// call's .raw + .wav plus the muxed output. Without a global budget, a handful
// of long video calls can fill the disk and break the AUDIO recording of
// unrelated meetings.
const VIDEO_MAX_TOTAL_BYTES = numericEnv(
    'VIDEO_MAX_TOTAL_BYTES',
    8 * 1024 * 1024 * 1024
);
// How long to wait for the audio side to finish after video ends (and vice
// versa is implicit: audio end triggers the mux directly).
const VIDEO_MUX_WAIT_FOR_AUDIO_MS = numericEnv(
    'VIDEO_MUX_WAIT_FOR_AUDIO_MS',
    60_000
);
const FFMPEG_TIMEOUT_MS = numericEnv('FFMPEG_TIMEOUT_MS', 10 * 60 * 1000);
// Test/debug only: keep the muxed MP4 on disk (skip the post-upload delete) so
// local E2E harnesses can inspect it. Never set in production.
const VIDEO_KEEP_MUXED =
    (process.env['VIDEO_KEEP_MUXED'] || 'false') === 'true';
// After a video socket drops without END_VIDEO, keep the session alive this
// long for the client to reconnect (a new START_VIDEO) before finalizing.
const VIDEO_RECONNECT_GRACE_MS = numericEnv('VIDEO_RECONNECT_GRACE_MS', 120_000);
// Absolute ceiling on a video session's lifetime, armed at START_VIDEO. A
// client that streams forever without ever sending END_VIDEO and without
// disconnecting would otherwise leak its session, file handle, temp file, and
// the adopted audio WAV until the task is replaced. 4h covers any real meeting.
const VIDEO_MAX_SESSION_MS = numericEnv('VIDEO_MAX_SESSION_MS', 4 * 60 * 60 * 1000);
// Once the audio side has finished, how long to keep waiting for END_VIDEO
// before finalizing anyway. Bounds the "audio ended, video never did" leak.
const VIDEO_WAIT_FOR_END_AFTER_AUDIO_MS = numericEnv(
    'VIDEO_WAIT_FOR_END_AFTER_AUDIO_MS',
    120_000
);

/** Total bytes currently written to disk across all live video sessions. */
let videoTotalBytes = 0;

const s3Client = new S3Client({ region: AWS_REGION });

export type VideoSession = {
    callId: string;
    // One fMP4 file per START_VIDEO (a reconnect re-sends the init segment,
    // which can't be appended mid-file; the mux concatenates segments).
    segmentPaths: string[];
    writeStream?: fs.WriteStream;
    fileSize: number;
    sizeCapReached: boolean;
    videoTimeOffsetMs: number;
    videoEnded: boolean;
    audioDone: boolean;
    audioWavPath?: string;
    audioSamplingRate?: number;
    muxStarted: boolean;
    discarding: boolean; // START_VIDEO received but video recording disabled
    tokens: {
        accessToken?: string;
        idToken?: string;
        refreshToken?: string;
    };
    safetyTimer?: NodeJS.Timeout;
    // Pending forced end (socket dropped without END_VIDEO, or audio finished
    // while the video stream is still live).
    graceTimer?: NodeJS.Timeout;
    // Absolute session lifetime cap, armed at START_VIDEO (see
    // VIDEO_MAX_SESSION_MS) so a client that never ends can't leak forever.
    lifetimeTimer?: NodeJS.Timeout;
    // True once the mux/upload has finished and cleanup has run. Guards against
    // a second cleanup unlinking files a running ffmpeg still needs.
    finalized: boolean;
    // One-shot flag so a slow disk logs once per session, not per frame.
    backpressureLogged?: boolean;
    /** Bytes discarded because they arrived with no open part file. */
    droppedBytes?: number;
    dropWarned?: boolean;
    /**
     * True when at least one reconnect continued the SAME client-side encoder
     * session across part files. Those parts are pieces of ONE fMP4 byte stream
     * (only the first carries the init segment), so the mux must concatenate the
     * bytes rather than treat each part as an independent MP4.
     */
    resumeParts?: boolean;
};

// Video sessions by callId. A session outlives its websocket: it is removed
// only after the mux/upload completes (or is abandoned).
const videoSessions = new Map<string, VideoSession>();

export const isVideoRecordingEnabled = (): boolean => ENABLE_VIDEO_RECORDING;

export const getVideoSession = (callId: string): VideoSession | undefined =>
    videoSessions.get(callId);

/**
 * Create a video segment write stream with an 'error' listener attached.
 *
 * Without a listener, an fs WriteStream that errors (ENOSPC when the shared
 * ephemeral volume fills, EDQUOT, a write after an async close) emits 'error'
 * with no handler, which Node turns into an uncaught exception — killing the
 * whole task and every concurrent call on it. Instead we mark the session
 * discarding and keep serving audio.
 */
const createSegmentStream = (
    session: VideoSession,
    segPath: string,
    server: FastifyInstance,
    append = false
): fs.WriteStream => {
    const stream = fs.createWriteStream(
        segPath,
        append ? { flags: 'a' } : undefined
    );
    stream.on('error', (err) => {
        server.log.error(
            `[VIDEO]: [${session.callId}] - Video write failed (${segPath}); abandoning video for this call, audio is unaffected: ${normalizeErrorForLogging(err)}`
        );
        // Stop accepting frames. Do NOT throw: the audio recording for this and
        // every other in-flight call must survive a full disk.
        session.discarding = true;
        if (session.writeStream === stream) {
            session.writeStream = undefined;
        }
    });
    return stream;
};

const segmentFileName = (callId: string, index: number): string =>
    `${posixifyFilename(callId)}_video_${index}.mp4`;

const muxedFileName = (callId: string): string =>
    `${posixifyFilename(callId)}.mp4`;

/**
 * Handle a START_VIDEO event. Returns the session the socket should be bound
 * to (so binary frames on that socket can be routed), or undefined when video
 * recording is disabled server-side (the caller should still bind the socket
 * to a discarding session so binary frames are dropped quietly).
 */
export const startVideoSession = (
    callMetaData: CallMetaData,
    server: FastifyInstance
): VideoSession => {
    const callId = callMetaData.callId;
    let session = videoSessions.get(callId);

    if (!ENABLE_VIDEO_RECORDING) {
        server.log.warn(
            `[START_VIDEO]: [${callId}] - Video recording is disabled (ENABLE_VIDEO_RECORDING=false). Discarding video stream.`
        );
        if (!session) {
            session = newSession(callId, callMetaData);
            session.discarding = true;
            videoSessions.set(callId, session);
        }
        return session;
    }

    if (session && !session.videoEnded && !session.muxStarted) {
        if (session.graceTimer) {
            clearTimeout(session.graceTimer);
            session.graceTimer = undefined;
        }
        // Close the previous stream. NOTE: end() is asynchronous and the old
        // stream tracks its own byte offset, so we must NOT immediately open an
        // O_APPEND stream on the same file — the two would clobber each other's
        // bytes at the seam. Instead every (re)connect gets its OWN part file,
        // and the mux concatenates them in order. This also keeps this function
        // synchronous, which matters: ws 'message' handlers are not serialized,
        // so an await here would let binary frames arrive before the socket is
        // bound to its session.
        session.writeStream?.end();
        session.writeStream = undefined;
        if (callMetaData.videoResume === true && session.segmentPaths.length > 0) {
            // Reconnect of the SAME client encoder session: the bytes continue
            // one fMP4 stream, so the parts must be concatenated BYTE-WISE (not
            // as independent MP4s) — recorded here for the mux.
            session.resumeParts = true;
            server.log.info(
                `[START_VIDEO]: [${callId}] - Video stream reconnected (resume); continuing into part ${
                    session.segmentPaths.length + 1
                }.`
            );
        } else {
            // Fresh client encoder session (e.g. app restarted mid-call): the
            // new part begins with its own fMP4 init segment, so the mux treats
            // the parts as independent MP4s (concat demuxer).
            server.log.info(
                `[START_VIDEO]: [${callId}] - Video stream restarted; starting part ${
                    session.segmentPaths.length + 1
                }`
            );
        }
    } else if (session) {
        server.log.warn(
            `[START_VIDEO]: [${callId}] - START_VIDEO received after video/mux already ended; discarding new stream.`
        );
        session.discarding = true;
        return session;
    } else {
        session = newSession(callId, callMetaData);
        videoSessions.set(callId, session);
        // Absolute lifetime cap: a client that streams forever and never sends
        // END_VIDEO (and never disconnects) would otherwise leak this session,
        // its fd, its temp file, and the adopted audio WAV until task replacement.
        const created = session;
        session.lifetimeTimer = setTimeout(() => {
            if (videoSessions.get(callId) !== created) {
                return; // superseded by a newer session for this callId
            }
            server.log.warn(
                `[VIDEO]: [${callId}] - Video session exceeded the maximum lifetime (${VIDEO_MAX_SESSION_MS}ms); finalizing.`
            );
            endVideoSession(callId, server).catch((err) =>
                server.log.error(
                    `[VIDEO]: [${callId}] - Error finalizing video at max lifetime: ${normalizeErrorForLogging(err)}`
                )
            );
        }, VIDEO_MAX_SESSION_MS);
        server.log.info(
            `[START_VIDEO]: [${callId}] - Video recording started (offsetMs=${session.videoTimeOffsetMs})`
        );
    }

    const segPath = path.resolve(
        LOCAL_TEMP_DIR,
        segmentFileName(callId, session.segmentPaths.length + 1)
    );
    session.segmentPaths.push(segPath);
    session.writeStream = createSegmentStream(session, segPath, server);
    return session;
};

const newSession = (
    callId: string,
    callMetaData: CallMetaData
): VideoSession => ({
    callId,
    segmentPaths: [],
    fileSize: 0,
    sizeCapReached: false,
    videoTimeOffsetMs:
        typeof callMetaData.videoTimeOffsetMs === 'number' &&
        isFinite(callMetaData.videoTimeOffsetMs)
            ? Math.max(0, callMetaData.videoTimeOffsetMs)
            : 0,
    videoEnded: false,
    audioDone: false,
    muxStarted: false,
    discarding: false,
    finalized: false,
    tokens: {
        accessToken: callMetaData.accessToken,
        idToken: callMetaData.idToken,
        refreshToken: callMetaData.refreshToken,
    },
});

/** Append a binary fMP4 chunk from the video socket. */
export const writeVideoChunk = (
    session: VideoSession,
    data: Uint8Array,
    server: FastifyInstance
): void => {
    if (session.discarding || session.videoEnded) {
        return;
    }
    if (!session.writeStream) {
        // No open part file: the socket dropped and we are inside the reconnect
        // window. Bytes arriving now would be silently lost, which corrupts the
        // stream from that point on — so count them and say so loudly.
        session.droppedBytes = (session.droppedBytes ?? 0) + data.length;
        if (!session.dropWarned) {
            session.dropWarned = true;
            server.log.error(
                `[VIDEO]: [${session.callId}] - Video bytes arrived with no open part file (reconnect window); they are being DROPPED and the recording will be truncated.`
            );
        }
        return;
    }
    // Two caps: this call's own budget, and a budget shared by every concurrent
    // call. The global one is what protects OTHER meetings' audio recordings
    // from one heavy video call filling the shared ephemeral volume.
    const overPerCall = session.fileSize + data.length > VIDEO_MAX_FILE_SIZE_BYTES;
    const overGlobal = videoTotalBytes + data.length > VIDEO_MAX_TOTAL_BYTES;
    if (overPerCall || overGlobal) {
        if (!session.sizeCapReached) {
            session.sizeCapReached = true;
            server.log.error(
                overGlobal
                    ? `[VIDEO]: [${session.callId}] - GLOBAL video disk budget reached (${VIDEO_MAX_TOTAL_BYTES} bytes across all calls). Dropping further video data for this call; recording will be truncated. Audio is unaffected.`
                    : `[VIDEO]: [${session.callId}] - Video size cap reached (${VIDEO_MAX_FILE_SIZE_BYTES} bytes). Dropping further video data; recording will be truncated.`
            );
        }
        return;
    }
    // write() returning false means the internal buffer is above the high-water
    // mark. We deliberately keep writing rather than buffering in our own queue
    // (that is what grows the heap), but we DO surface it once so a slow disk is
    // diagnosable, and the size caps above bound the total outstanding bytes.
    const ok = session.writeStream.write(data);
    if (!ok && !session.backpressureLogged) {
        session.backpressureLogged = true;
        server.log.warn(
            `[VIDEO]: [${session.callId}] - Video disk writes are behind the incoming stream (backpressure). Recording continues; check ephemeral storage throughput.`
        );
    }
    session.fileSize += data.length;
    videoTotalBytes += data.length;
};

/**
 * The video socket dropped without an explicit END_VIDEO (crash / network
 * blip). Keep the session open for a grace period so a reconnecting client
 * (fresh START_VIDEO) can resume with a new segment; finalize if it doesn't.
 */
export const videoSocketDropped = (
    callId: string,
    server: FastifyInstance
): void => {
    const session = videoSessions.get(callId);
    if (!session || session.videoEnded || session.muxStarted) {
        return;
    }
    // Flush what we have so the segment on disk is complete up to the drop.
    session.writeStream?.end();
    session.writeStream = undefined;
    if (session.graceTimer) {
        return; // already counting down
    }
    server.log.info(
        `[VIDEO]: [${callId}] - Video socket dropped without END_VIDEO; waiting up to ${VIDEO_RECONNECT_GRACE_MS}ms for reconnect.`
    );
    const dropped = session;
    session.graceTimer = setTimeout(() => {
        dropped.graceTimer = undefined;
        // The timer handle lives on THIS session object, but the callId may have
        // been reused by a newer session in the meantime — finalizing then would
        // truncate a live recording. Only act if we are still the current one.
        if (videoSessions.get(callId) !== dropped) {
            return;
        }
        endVideoSession(callId, server).catch((err) =>
            server.log.error(
                `[VIDEO]: [${callId}] - Error finalizing video after reconnect grace period: ${normalizeErrorForLogging(err)}`
            )
        );
    }, VIDEO_RECONNECT_GRACE_MS);
};

/**
 * Handle END_VIDEO (or expiry of the reconnect grace period). If the audio
 * side has already finished, mux now; otherwise wait for it (with a safety
 * timeout).
 */
export const endVideoSession = async (
    callId: string,
    server: FastifyInstance
): Promise<void> => {
    const session = videoSessions.get(callId);
    if (!session || session.videoEnded) {
        return;
    }
    if (session.graceTimer) {
        clearTimeout(session.graceTimer);
        session.graceTimer = undefined;
    }
    session.videoEnded = true;
    // Await the flush so ffmpeg never reads a truncated last segment.
    const ws = session.writeStream;
    session.writeStream = undefined;
    if (ws) {
        await new Promise<void>((resolve) => ws.end(() => resolve()));
    }

    // Nothing usable was recorded. Only clean up if a mux is NOT already in
    // flight: the audio side can have started one during the flush await above,
    // and unlinking its inputs would make ffmpeg fail or emit a 0-byte MP4.
    if (session.discarding || session.fileSize === 0) {
        if (session.muxStarted) {
            server.log.info(
                `[END_VIDEO]: [${callId}] - No video data, but a mux is already running; leaving cleanup to it.`
            );
            return;
        }
        server.log.info(
            `[END_VIDEO]: [${callId}] - Video session ended with no recorded data; cleaning up.`
        );
        await cleanupSession(session, server);
        return;
    }

    server.log.info(
        `[END_VIDEO]: [${callId}] - Video stream ended (${session.fileSize} bytes over ${session.segmentPaths.length} part(s))${
            session.droppedBytes ? `; ${session.droppedBytes} bytes were DROPPED during a reconnect gap` : ''
        }.`
    );

    if (session.audioDone) {
        await muxAndUpload(session, server);
    } else {
        // Audio side still in flight — it will trigger the mux from
        // notifyAudioRecordingDone(). Safety net if it never does.
        session.safetyTimer = setTimeout(() => {
            server.log.warn(
                `[VIDEO]: [${callId}] - Audio side did not complete within ${VIDEO_MUX_WAIT_FOR_AUDIO_MS}ms of video end; muxing without audio.`
            );
            muxAndUpload(session, server).catch((err) =>
                server.log.error(
                    `[VIDEO]: [${callId}] - Deferred mux failed: ${normalizeErrorForLogging(err)}`
                )
            );
        }, VIDEO_MUX_WAIT_FOR_AUDIO_MS);
    }
};

/**
 * Called by the audio endCall path when its recording work is finished.
 * wavPath is the local WAV file when audio recording produced one (the caller
 * must NOT delete it if this returns true — the mux owns it now), or
 * undefined when audio recording was disabled.
 * Returns true if a video session adopted the WAV file.
 */
export const notifyAudioRecordingDone = (
    callId: string,
    wavPath: string | undefined,
    server: FastifyInstance
): boolean => {
    const session = videoSessions.get(callId);
    if (!session || session.muxStarted || session.discarding) {
        return false;
    }
    session.audioDone = true;
    session.audioWavPath = wavPath;

    if (session.videoEnded) {
        muxAndUpload(session, server).catch((err) =>
            server.log.error(
                `[VIDEO]: [${callId}] - Mux failed: ${normalizeErrorForLogging(err)}`
            )
        );
    } else if (!session.graceTimer) {
        // The call's audio is done but the video stream is still open. Bound the
        // wait: without this, a client that never sends END_VIDEO leaks the
        // session AND the audio WAV it just adopted (we told the caller not to
        // delete it), with no timer to ever release them.
        const waiting = session;
        session.graceTimer = setTimeout(() => {
            waiting.graceTimer = undefined;
            if (videoSessions.get(callId) !== waiting) {
                return;
            }
            server.log.warn(
                `[VIDEO]: [${callId}] - Audio ended ${VIDEO_WAIT_FOR_END_AFTER_AUDIO_MS}ms ago and END_VIDEO never arrived; finalizing video now.`
            );
            endVideoSession(callId, server).catch((err) =>
                server.log.error(
                    `[VIDEO]: [${callId}] - Error finalizing video after audio-end wait: ${normalizeErrorForLogging(err)}`
                )
            );
        }, VIDEO_WAIT_FOR_END_AFTER_AUDIO_MS);
    }
    // Session adopted the wav (it will delete it after muxing).
    return wavPath !== undefined;
};

const muxAndUpload = async (
    session: VideoSession,
    server: FastifyInstance
): Promise<void> => {
    // Node is single-threaded: this check-and-set is atomic w.r.t. the other
    // trigger paths (END_VIDEO handler, audio-done callback, safety timer).
    if (session.muxStarted) {
        return;
    }
    session.muxStarted = true;
    if (session.safetyTimer) {
        clearTimeout(session.safetyTimer);
        session.safetyTimer = undefined;
    }
    if (session.graceTimer) {
        clearTimeout(session.graceTimer);
        session.graceTimer = undefined;
    }
    if (session.lifetimeTimer) {
        clearTimeout(session.lifetimeTimer);
        session.lifetimeTimer = undefined;
    }
    // Nothing to mux. Publishing a 0-byte MP4 would put a permanently broken
    // <video> on the call detail page, which is worse than no video at all.
    if (session.fileSize === 0 || session.segmentPaths.length === 0) {
        server.log.warn(
            `[VIDEO]: [${session.callId}] - No video data recorded; skipping mux and upload.`
        );
        await cleanupSession(session, server);
        return;
    }

    const callId = session.callId;
    const outFileName = muxedFileName(callId);
    const outPath = path.resolve(LOCAL_TEMP_DIR, outFileName);

    try {
        // Resumed parts are pieces of ONE fMP4 byte stream (only part 1 has the
        // init segment), so they must be joined byte-wise BEFORE ffmpeg sees
        // them. Independent parts (a fresh encoder session per reconnect) are
        // separate MP4s and are handled by ffmpeg's concat demuxer instead.
        if (session.resumeParts && session.segmentPaths.length > 1) {
            await joinResumeParts(session, server);
        }
        try {
            await runFfmpegMux(session, outPath, server);
        } catch (ffmpegErr) {
            server.log.error(
                `[VIDEO]: [${callId}] - ffmpeg mux failed, falling back to uploading raw video stream (no audio track): ${normalizeErrorForLogging(
                    ffmpegErr
                )}`
            );
            // The fMP4 stream as received is still a playable MP4 (the init
            // segment carries the moov). Better a silent video than nothing.
            // After joinResumeParts, part 1 holds the whole resumed stream; for
            // independent parts only the first is usable this way, so say so
            // rather than silently truncating.
            if (!session.resumeParts && session.segmentPaths.length > 1) {
                server.log.error(
                    `[VIDEO]: [${session.callId}] - Fallback keeps only part 1 of ${session.segmentPaths.length}; video after the first restart is lost.`
                );
            }
            const first = session.segmentPaths[0];
            if (!fs.existsSync(first)) {
                // Nothing left to salvage (the inputs are gone). Give up on the
                // video rather than throwing: an exception here would propagate
                // into the websocket message handler, whose catch calls
                // process.exit(1) and would drop every concurrent call.
                server.log.error(
                    `[VIDEO]: [${session.callId}] - Fallback source is missing (${first}); abandoning video for this call.`
                );
                return;
            }
            await fs.promises.copyFile(first, outPath);
        }

        const videoUrl = await uploadVideo(outPath, outFileName, server, callId);
        if (videoUrl) {
            const callMetaData = {
                callId,
                accessToken: session.tokens.accessToken,
                idToken: session.tokens.idToken,
                refreshToken: session.tokens.refreshToken,
            } as CallMetaData;
            await writeCallVideoRecordingEvent(callMetaData, videoUrl, server);
            server.log.info(
                `[VIDEO]: [${callId}] - Video recording uploaded and ADD_S3_VIDEO_RECORDING_URL written to KDS: ${videoUrl}`
            );
        }
    } finally {
        if (VIDEO_KEEP_MUXED) {
            server.log.info(`[VIDEO]: [${callId}] - VIDEO_KEEP_MUXED set; keeping ${outPath}`);
        } else {
            await deleteQuietly(outPath, server, callId);
        }
        await cleanupSession(session, server);
    }
};

/**
 * Append parts 2..N onto part 1, in order, and collapse segmentPaths to just
 * part 1. Used when a reconnect continued the SAME client encoder session: the
 * parts are consecutive slices of one fMP4 byte stream, so simple byte
 * concatenation reproduces the original stream. (Independent MP4s must NOT be
 * joined this way — those go through ffmpeg's concat demuxer.)
 */
const joinResumeParts = async (
    session: VideoSession,
    server: FastifyInstance
): Promise<void> => {
    const [first, ...rest] = session.segmentPaths;
    const out = fs.createWriteStream(first, { flags: 'a' });
    try {
        for (const part of rest) {
            if (!fs.existsSync(part)) {
                continue;
            }
            await new Promise<void>((resolve, reject) => {
                const input = fs.createReadStream(part);
                input.on('error', reject);
                input.on('end', () => resolve());
                input.pipe(out, { end: false });
            });
        }
    } finally {
        await new Promise<void>((resolve) => out.end(() => resolve()));
    }
    for (const part of rest) {
        await deleteQuietly(part, server, session.callId);
    }
    session.segmentPaths = [first];
    server.log.info(
        `[VIDEO]: [${session.callId}] - Joined ${rest.length + 1} resumed video parts into one stream.`
    );
};

// Exported for the smoke test (test/video-recording.smoke.ts), which
// exercises the ffmpeg invocation with real generated media.
export const runFfmpegMux = (
    session: VideoSession,
    outPath: string,
    server: FastifyInstance
): Promise<void> => {
    const args: string[] = ['-y', '-nostdin', '-loglevel', 'error'];
    let concatListPath: string | undefined;

    // Video input: single segment directly, multiple segments via the concat
    // demuxer (same encoder/codec params across segments).
    if (session.segmentPaths.length === 1) {
        if (session.videoTimeOffsetMs > 100) {
            args.push('-itsoffset', (session.videoTimeOffsetMs / 1000).toFixed(3));
        }
        args.push('-i', session.segmentPaths[0]);
    } else {
        concatListPath = path.resolve(
            LOCAL_TEMP_DIR,
            `${posixifyFilename(session.callId)}_video_concat.txt`
        );
        const listBody = session.segmentPaths
            .map((p) => `file '${p.replace(/'/g, '\'\\\'\'')}'`)
            .join('\n');
        // Sync I/O here would stall every other call's audio ingest.
        fs.writeFileSync(concatListPath, listBody);
        if (session.videoTimeOffsetMs > 100) {
            args.push('-itsoffset', (session.videoTimeOffsetMs / 1000).toFixed(3));
        }
        args.push('-f', 'concat', '-safe', '0', '-i', concatListPath);
    }

    // Optional audio input (the call's WAV recording).
    if (session.audioWavPath) {
        args.push('-i', session.audioWavPath);
        args.push('-c:v', 'copy', '-c:a', 'aac', '-b:a', '96k');
    } else {
        args.push('-c', 'copy');
    }
    args.push('-movflags', '+faststart', outPath);

    server.log.info(
        `[VIDEO]: [${session.callId}] - Running mux: ${FFMPEG_PATH} ${args.join(' ')}`
    );

    return new Promise<void>((resolve, reject) => {
        const proc = spawn(FFMPEG_PATH, args, {
            stdio: ['ignore', 'ignore', 'pipe'],
        });
        let stderr = '';
        proc.stderr.on('data', (d) => {
            stderr += d.toString();
            if (stderr.length > 8192) {
                stderr = stderr.slice(-8192);
            }
        });
        const killTimer = setTimeout(() => {
            proc.kill('SIGKILL');
        }, FFMPEG_TIMEOUT_MS);
        proc.on('error', (err) => {
            clearTimeout(killTimer);
            reject(err);
        });
        proc.on('close', (code) => {
            clearTimeout(killTimer);
            if (concatListPath) {
                fs.promises.unlink(concatListPath).catch(() => undefined);
            }
            if (code === 0) {
                resolve();
            } else {
                reject(
                    new Error(`ffmpeg exited with code ${code}: ${stderr.trim()}`)
                );
            }
        });
    });
};

const uploadVideo = async (
    filePath: string,
    fileName: string,
    server: FastifyInstance,
    callId: string
): Promise<string | undefined> => {
    const fileStream = fs.createReadStream(filePath);
    try {
        await s3Client.send(
            new PutObjectCommand({
                Bucket: RECORDINGS_BUCKET_NAME,
                Key: VIDEO_RECORDINGS_KEY_PREFIX + fileName,
                Body: fileStream,
                ContentType: 'video/mp4',
            })
        );
        // Same URL shape as the audio recording and the Virtual Participant:
        // the UI presigns it client-side for playback.
        return `https://${RECORDINGS_BUCKET_NAME}.s3.${AWS_REGION}.amazonaws.com/${VIDEO_RECORDINGS_KEY_PREFIX}${fileName}`;
    } catch (err) {
        server.log.error(
            `[VIDEO]: [${callId}] - Error uploading video recording to S3: ${normalizeErrorForLogging(
                err
            )}`
        );
        return undefined;
    } finally {
        fileStream.destroy();
    }
};

const cleanupSession = async (
    session: VideoSession,
    server: FastifyInstance
): Promise<void> => {
    // Idempotent: several paths can reach cleanup for the same session, and a
    // second pass must not unlink files a running ffmpeg still holds.
    if (session.finalized) {
        return;
    }
    session.finalized = true;
    if (session.safetyTimer) {
        clearTimeout(session.safetyTimer);
        session.safetyTimer = undefined;
    }
    if (session.graceTimer) {
        clearTimeout(session.graceTimer);
        session.graceTimer = undefined;
    }
    if (session.lifetimeTimer) {
        clearTimeout(session.lifetimeTimer);
        session.lifetimeTimer = undefined;
    }
    // Release this session's share of the global disk budget.
    videoTotalBytes = Math.max(0, videoTotalBytes - session.fileSize);
    session.writeStream?.end();
    session.writeStream = undefined;
    for (const segPath of session.segmentPaths) {
        await deleteQuietly(segPath, server, session.callId);
    }
    if (session.audioWavPath) {
        await deleteQuietly(session.audioWavPath, server, session.callId);
        session.audioWavPath = undefined;
    }
    videoSessions.delete(session.callId);
};

const deleteQuietly = async (
    filePath: string,
    server: FastifyInstance,
    callId: string
): Promise<void> => {
    if (!filePath.startsWith(LOCAL_TEMP_DIR)) {
        server.log.error(
            `[VIDEO]: [${callId}] - Refusing to delete file outside temp dir: ${filePath}`
        );
        return;
    }
    try {
        await fs.promises.unlink(filePath);
    } catch {
        // already gone / never created — fine
    }
};
