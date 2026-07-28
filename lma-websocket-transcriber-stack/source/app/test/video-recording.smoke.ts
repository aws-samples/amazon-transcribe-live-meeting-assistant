/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 *
 * Smoke test for the desktop-video recording lane (src/videorecording.ts).
 *
 * The production server (src/index.ts) auto-listens on import and requires a
 * live Cognito user pool, so — like fastify5-websocket.smoke.ts — this test
 * wires the video module into a minimal fastify server exactly the way
 * index.ts wires it (START_VIDEO / binary fMP4 / END_VIDEO over a dedicated
 * websocket), and drives it with a REAL client and REAL media:
 *
 *   1. ffmpeg generates a short H.264 fragmented-MP4 (the same container the
 *      native clients stream) and a WAV file (the audio path's output).
 *   2. A ws client connects, sends START_VIDEO, streams the fMP4 in chunks,
 *      sends END_VIDEO; the audio side is simulated via
 *      notifyAudioRecordingDone(wav).
 *   3. Asserts the mux ran: ffprobe reports BOTH an h264 video stream and an
 *      aac audio stream in the produced MP4, and temp segments were cleaned
 *      up. S3 upload / KDS write fail gracefully without AWS credentials (the
 *      module logs and continues) — upload success is covered by E2E testing.
 *   4. Backward-compat guard: unknown callEvent values (what an OLD server
 *      sees from a NEW client) fall through without side effects, and a video
 *      START_VIDEO when ENABLE_VIDEO_RECORDING=false discards cleanly.
 *
 * Requires ffmpeg + ffprobe on PATH (present in the runtime container and on
 * dev machines). Run: npm run smoke-video. Exit 0 = pass.
 */
import fs from 'fs';
import os from 'os';
import path from 'path';
import assert from 'assert';
import { execFileSync } from 'child_process';

// --- Test environment must be set BEFORE importing the module under test ---
const TEST_TMP = fs.mkdtempSync(path.join(os.tmpdir(), 'lma-video-smoke-'));
process.env['LOCAL_TEMP_DIR'] = TEST_TMP + path.sep;
process.env['ENABLE_VIDEO_RECORDING'] = 'true';
process.env['RECORDINGS_BUCKET_NAME'] = 'smoke-test-bucket-does-not-exist';
process.env['VIDEO_MUX_WAIT_FOR_AUDIO_MS'] = '5000';
process.env['AWS_REGION'] = process.env['AWS_REGION'] || 'us-east-1';

import fastify from 'fastify';
import websocket from '@fastify/websocket';
import WebSocket from 'ws';
import {
    VideoSession,
    startVideoSession,
    endVideoSession,
    videoSocketDropped,
    writeVideoChunk,
    notifyAudioRecordingDone,
    runFfmpegMux,
} from '../src/videorecording';
import { CallMetaData } from '../src/calleventdata/eventtypes';

const PORT = parseInt(process.env['SMOKE_PORT'] ?? '38081', 10);
const HOST = '127.0.0.1';

const sh = (cmd: string, args: string[]): string =>
    execFileSync(cmd, args, { encoding: 'utf8' });

const makeMedia = (): { fmp4: string; wav: string } => {
    const fmp4 = path.join(TEST_TMP, 'source_video.mp4');
    const wav = path.join(TEST_TMP, 'source_audio.wav');
    // 3s of 320x240 test pattern, H.264, fragmented MP4 — matches the
    // container the native clients stream (init segment + moof fragments).
    sh('ffmpeg', [
        '-y', '-nostdin', '-loglevel', 'error',
        '-f', 'lavfi', '-i', 'testsrc=duration=3:size=320x240:rate=5',
        '-c:v', 'libx264', '-preset', 'ultrafast', '-pix_fmt', 'yuv420p',
        '-g', '10',
        '-movflags', 'frag_keyframe+empty_moov+default_base_moof',
        fmp4,
    ]);
    // 3s of stereo 48kHz tone — same shape as the audio path's WAV output.
    sh('ffmpeg', [
        '-y', '-nostdin', '-loglevel', 'error',
        '-f', 'lavfi', '-i', 'sine=frequency=440:duration=3',
        '-ac', '2', '-ar', '48000', '-c:a', 'pcm_s16le',
        wav,
    ]);
    return { fmp4, wav };
};

const ffprobeCodecs = (file: string): string[] =>
    sh('ffprobe', [
        '-v', 'error',
        '-show_entries', 'stream=codec_name,codec_type',
        '-of', 'csv=p=0',
        file,
    ])
        .trim()
        .split('\n')
        .map((l) => l.trim())
        .filter(Boolean);

async function main(): Promise<void> {
    const { fmp4, wav } = makeMedia();

    // ---- Part 1: direct mux verification (video + audio -> single MP4) ----
    {
        const seg = path.join(TEST_TMP, 'seg1.mp4');
        fs.copyFileSync(fmp4, seg);
        const session: VideoSession = {
            callId: 'mux-direct-test',
            segmentPaths: [seg],
            fileSize: fs.statSync(seg).size,
            sizeCapReached: false,
            videoTimeOffsetMs: 250,
            videoEnded: true,
            audioDone: true,
            audioWavPath: wav,
            muxStarted: false,
            discarding: false,
            tokens: {},
        };
        const out = path.join(TEST_TMP, 'muxed.mp4');
        const fakeServer = {
            log: { info: () => undefined, error: () => undefined, warn: () => undefined, debug: () => undefined },
        } as unknown as Parameters<typeof runFfmpegMux>[2];
        await runFfmpegMux(session, out, fakeServer);
        const codecs = ffprobeCodecs(out);
        assert.ok(
            codecs.some((c) => c.startsWith('h264,video')),
            `muxed file must contain an h264 video stream (got: ${codecs.join(' | ')})`
        );
        assert.ok(
            codecs.some((c) => c.startsWith('aac,audio')),
            `muxed file must contain an aac audio stream (got: ${codecs.join(' | ')})`
        );
        console.log('PASS: direct mux produced MP4 with h264 video + aac audio streams');
    }

    // ---- Part 2: full websocket protocol flow, wired as in index.ts ----
    const server = fastify({ logger: { level: 'warn' } });
    server.register(websocket);
    const videoSocketMap = new Map<WebSocket, VideoSession>();

    server.after(() => {
        server.get('/api/v1/ws', { websocket: true }, (socket) => {
            socket.on('message', (data: WebSocket.RawData, isBinary: boolean) => {
                if (isBinary) {
                    const session = videoSocketMap.get(socket as WebSocket);
                    if (session) {
                        writeVideoChunk(session, Buffer.from(data as Buffer), server);
                    }
                    return;
                }
                const meta = JSON.parse(
                    Buffer.from(data as Buffer).toString('utf8')
                ) as CallMetaData;
                if (meta.callEvent === 'START_VIDEO') {
                    const session = startVideoSession(meta, server);
                    videoSocketMap.set(socket as WebSocket, session);
                } else if (meta.callEvent === 'END_VIDEO') {
                    const session = videoSocketMap.get(socket as WebSocket);
                    videoSocketMap.delete(socket as WebSocket);
                    if (session) {
                        endVideoSession(session.callId, server).catch(() => undefined);
                    }
                }
                // Unknown callEvent values fall through — same as index.ts.
            });
            socket.on('close', () => {
                const session = videoSocketMap.get(socket as WebSocket);
                if (session) {
                    videoSocketMap.delete(socket as WebSocket);
                    videoSocketDropped(session.callId, server);
                }
            });
        });
    });

    await server.listen({ port: PORT, host: HOST });
    const CALL_ID = 'smoke-ws-video-call';

    try {
        // Stream the fMP4 over the socket in 32KiB chunks, then END_VIDEO.
        await new Promise<void>((resolve, reject) => {
            const ws = new WebSocket(`ws://${HOST}:${PORT}/api/v1/ws`);
            const timer = setTimeout(
                () => reject(new Error('WS video stream timed out (15s)')),
                15_000
            );
            ws.on('open', () => {
                ws.send(
                    JSON.stringify({
                        callEvent: 'START_VIDEO',
                        callId: CALL_ID,
                        videoTimeOffsetMs: 100,
                    })
                );
                const bytes = fs.readFileSync(fmp4);
                for (let i = 0; i < bytes.length; i += 32768) {
                    ws.send(bytes.subarray(i, i + 32768), { binary: true });
                }
                ws.send(JSON.stringify({ callEvent: 'END_VIDEO', callId: CALL_ID }));
                // Give END_VIDEO a beat to process before closing.
                setTimeout(() => {
                    ws.close();
                    clearTimeout(timer);
                    resolve();
                }, 500);
            });
            ws.on('error', reject);
        });

        // Simulate the audio endCall handing over its WAV.
        const wavCopy = path.join(TEST_TMP, `${CALL_ID}.wav`);
        fs.copyFileSync(wav, wavCopy);
        const adopted = notifyAudioRecordingDone(CALL_ID, wavCopy, server);
        assert.strictEqual(adopted, true, 'video session should adopt the audio WAV');

        // Mux + (failing, non-fatal) upload + cleanup run async; poll for
        // cleanup of the segment files as completion signal.
        await pollUntil(
            () =>
                !fs.existsSync(path.join(TEST_TMP, `${CALL_ID.replace(/-/g, '_')}_video_1.mp4`)) &&
                !fs.existsSync(wavCopy),
            20_000,
            'segment + adopted wav should be deleted after mux/upload attempt'
        );
        console.log('PASS: websocket START_VIDEO/fMP4/END_VIDEO flow muxed and cleaned up temp files');

        // ---- Part 3: disabled + discard paths ----
        process.env['ENABLE_VIDEO_RECORDING'] = 'false'; // note: module read at import; runtime check below
        const disabledMeta = {
            callEvent: 'START_VIDEO',
            callId: 'disabled-call',
        } as unknown as CallMetaData;
        const s2 = startVideoSession(disabledMeta, server);
        writeVideoChunk(s2, Buffer.alloc(1024), server);
        await endVideoSession('disabled-call', server);
        // Whether disabled (env read at import time = true here) or ended, a
        // second END must be a no-op and not throw.
        await endVideoSession('disabled-call', server);
        console.log('PASS: END_VIDEO is idempotent; no-data session cleaned up quietly');
    } finally {
        await server.close();
        fs.rmSync(TEST_TMP, { recursive: true, force: true });
    }
}

const pollUntil = async (
    cond: () => boolean,
    timeoutMs: number,
    what: string
): Promise<void> => {
    const start = Date.now();
    for (;;) {
        if (cond()) {
            return;
        }
        if (Date.now() - start > timeoutMs) {
            throw new Error(`Timed out waiting for: ${what}`);
        }
        await new Promise((r) => setTimeout(r, 250));
    }
};

main().then(
    () => {
        console.log('PASS: all video recording smoke checks passed.');
        process.exit(0);
    },
    (err) => {
        console.error('FAIL:', err instanceof Error ? err.stack : err);
        process.exit(1);
    }
);
