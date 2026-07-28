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
// Cap the on-disk video size per call (Fargate ephemeral storage is shared by
// all concurrent calls). ~1 Mbps screen content is ~450 MB/hour.
const VIDEO_MAX_FILE_SIZE_BYTES = parseInt(
    process.env['VIDEO_MAX_FILE_SIZE_BYTES'] || `${2 * 1024 * 1024 * 1024}`,
    10
);
// How long to wait for the audio side to finish after video ends (and vice
// versa is implicit: audio end triggers the mux directly).
const VIDEO_MUX_WAIT_FOR_AUDIO_MS = parseInt(
    process.env['VIDEO_MUX_WAIT_FOR_AUDIO_MS'] || '60000',
    10
);
const FFMPEG_TIMEOUT_MS = parseInt(
    process.env['FFMPEG_TIMEOUT_MS'] || `${10 * 60 * 1000}`,
    10
);
// Test/debug only: keep the muxed MP4 on disk (skip the post-upload delete) so
// local E2E harnesses can inspect it. Never set in production.
const VIDEO_KEEP_MUXED =
    (process.env['VIDEO_KEEP_MUXED'] || 'false') === 'true';
// After a video socket drops without END_VIDEO, keep the session alive this
// long for the client to reconnect (a new START_VIDEO) before finalizing.
const VIDEO_RECONNECT_GRACE_MS = parseInt(
    process.env['VIDEO_RECONNECT_GRACE_MS'] || '120000',
    10
);

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
};

// Video sessions by callId. A session outlives its websocket: it is removed
// only after the mux/upload completes (or is abandoned).
const videoSessions = new Map<string, VideoSession>();

export const isVideoRecordingEnabled = (): boolean => ENABLE_VIDEO_RECORDING;

export const getVideoSession = (callId: string): VideoSession | undefined =>
    videoSessions.get(callId);

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
        session.writeStream?.end();
        if (callMetaData.videoResume === true && session.segmentPaths.length > 0) {
            // Reconnect of the SAME client encoder session: the incoming bytes
            // continue the same fMP4 stream, so append to the current file.
            const segPath = session.segmentPaths[session.segmentPaths.length - 1];
            session.writeStream = fs.createWriteStream(segPath, { flags: 'a' });
            server.log.info(
                `[START_VIDEO]: [${callId}] - Video stream reconnected (resume); appending to current segment.`
            );
            return session;
        }
        // Fresh client encoder session (e.g. app restarted mid-call): rotate
        // to a new segment file — it begins with its own fMP4 init segment.
        server.log.info(
            `[START_VIDEO]: [${callId}] - Video stream restarted; starting segment ${
                session.segmentPaths.length + 1
            }`
        );
    } else if (session) {
        server.log.warn(
            `[START_VIDEO]: [${callId}] - START_VIDEO received after video/mux already ended; discarding new stream.`
        );
        session.discarding = true;
        return session;
    } else {
        session = newSession(callId, callMetaData);
        videoSessions.set(callId, session);
        server.log.info(
            `[START_VIDEO]: [${callId}] - Video recording started (offsetMs=${session.videoTimeOffsetMs})`
        );
    }

    const segPath = path.resolve(
        LOCAL_TEMP_DIR,
        segmentFileName(callId, session.segmentPaths.length + 1)
    );
    session.segmentPaths.push(segPath);
    session.writeStream = fs.createWriteStream(segPath);
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
    if (session.discarding || session.videoEnded || !session.writeStream) {
        return;
    }
    if (session.fileSize + data.length > VIDEO_MAX_FILE_SIZE_BYTES) {
        if (!session.sizeCapReached) {
            session.sizeCapReached = true;
            server.log.error(
                `[VIDEO]: [${session.callId}] - Video size cap reached (${VIDEO_MAX_FILE_SIZE_BYTES} bytes). Dropping further video data; recording will be truncated.`
            );
        }
        return;
    }
    session.writeStream.write(data);
    session.fileSize += data.length;
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
    session.graceTimer = setTimeout(() => {
        session.graceTimer = undefined;
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

    if (session.discarding || session.fileSize === 0) {
        server.log.info(
            `[END_VIDEO]: [${callId}] - Video session ended with no recorded data; cleaning up.`
        );
        await cleanupSession(session, server);
        return;
    }

    server.log.info(
        `[END_VIDEO]: [${callId}] - Video stream ended (${session.fileSize} bytes over ${session.segmentPaths.length} segment(s)).`
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

    const callId = session.callId;
    const outFileName = muxedFileName(callId);
    const outPath = path.resolve(LOCAL_TEMP_DIR, outFileName);

    try {
        try {
            await runFfmpegMux(session, outPath, server);
        } catch (ffmpegErr) {
            server.log.error(
                `[VIDEO]: [${callId}] - ffmpeg mux failed, falling back to uploading raw video stream (no audio track): ${normalizeErrorForLogging(
                    ffmpegErr
                )}`
            );
            // The fMP4 stream as received is still a playable MP4 (init
            // segment carries the moov). Better a silent video than nothing.
            await fs.promises.copyFile(session.segmentPaths[0], outPath);
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
    if (session.safetyTimer) {
        clearTimeout(session.safetyTimer);
        session.safetyTimer = undefined;
    }
    if (session.graceTimer) {
        clearTimeout(session.graceTimer);
        session.graceTimer = undefined;
    }
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
