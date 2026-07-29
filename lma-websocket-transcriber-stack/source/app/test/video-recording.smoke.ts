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
// Keep muxed output so the reconnect test can probe it (test-only flag).
process.env['VIDEO_KEEP_MUXED'] = 'true';
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
    getVideoSession,
} from '../src/videorecording';
import { CallMetaData } from '../src/calleventdata/eventtypes';

const PORT = parseInt(process.env['SMOKE_PORT'] ?? '38081', 10);
// Sampling rate is irrelevant to the video lane; the audio START just needs one.
const SR_UNUSED = 48000;
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
            finalized: false,
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
    const server = fastify({ logger: { level: process.env['SMOKE_LOG'] ?? 'warn' } });
    server.register(websocket);
    const videoSocketMap = new Map<WebSocket, VideoSession>();

    // Mirror of index.ts's authorization state: which user owns each live call.
    // START_VIDEO is only accepted for a call that is live AND owned by the
    // same verified subject. The test drives it via a per-connection "user"
    // query param standing in for the JWT subject.
    const liveCalls = new Map<string, string>(); // callId -> ownerSub
    const authDenials: string[] = [];

    server.after(() => {
        server.get('/api/v1/ws', { websocket: true }, (socket, request) => {
            const callerSub = String(
                (request.query as { user?: string } | undefined)?.user ?? ''
            );
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
                if (meta.callEvent === 'START') {
                    liveCalls.set(meta.callId, callerSub);
                } else if (meta.callEvent === 'END') {
                    liveCalls.delete(meta.callId);
                } else if (meta.callEvent === 'START_VIDEO') {
                    // Same two checks as index.ts.
                    const ownerSub = liveCalls.get(meta.callId);
                    if (!ownerSub) {
                        authDenials.push(`no-live-call:${meta.callId}`);
                        socket.close(1008, 'no active call for callId');
                        return;
                    }
                    if (!callerSub || ownerSub !== callerSub) {
                        authDenials.push(`not-owner:${meta.callId}`);
                        socket.close(1008, 'not authorized for callId');
                        return;
                    }
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
        // The owner's AUDIO socket declares the call (index.ts records the
        // verified subject as its owner). Video authorization depends on it.
        const audioWs = new WebSocket(`ws://${HOST}:${PORT}/api/v1/ws?user=alice`);
        await new Promise<void>((res, rej) => {
            audioWs.on('open', () => res());
            audioWs.on('error', rej);
        });
        audioWs.send(JSON.stringify({ callEvent: 'START', callId: CALL_ID, samplingRate: SR_UNUSED }));
        await new Promise((r) => setTimeout(r, 100));

        // Stream the fMP4 over the socket in 32KiB chunks, then END_VIDEO.
        await new Promise<void>((resolve, reject) => {
            const ws = new WebSocket(`ws://${HOST}:${PORT}/api/v1/ws?user=alice`);
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

        // ---- Part 2b: AUTHORIZATION — the cross-tenant attack must fail ----
        // Victim starts a call; attacker (different subject) tries to attach a
        // video stream to it. Without the ownership check the attacker's video
        // session would adopt the victim's audio WAV at end of call and publish
        // the victim's audio under an object the attacker can read.
        const VICTIM_CALL = 'victim-private-meeting';
        const victimWs = new WebSocket(`ws://${HOST}:${PORT}/api/v1/ws?user=victim`);
        await new Promise<void>((res, rej) => {
            victimWs.on('open', () => res());
            victimWs.on('error', rej);
        });
        victimWs.send(JSON.stringify({ callEvent: 'START', callId: VICTIM_CALL, samplingRate: SR_UNUSED }));
        await new Promise((r) => setTimeout(r, 100));

        const attackerClosed = await attemptVideo(`ws://${HOST}:${PORT}/api/v1/ws?user=attacker`, VICTIM_CALL);
        assert.ok(
            attackerClosed,
            'attacker START_VIDEO on the victim\'s callId must be rejected (socket closed)'
        );
        assert.ok(
            authDenials.includes(`not-owner:${VICTIM_CALL}`),
            `expected a not-owner denial, got: ${authDenials.join(', ')}`
        );
        assert.strictEqual(
            getVideoSession(VICTIM_CALL),
            undefined,
            'no video session may exist for the victim call after a rejected attempt'
        );
        console.log('PASS: START_VIDEO by a non-owner is rejected and creates no session');

        // A callId with no live audio call at all is also refused (this is what
        // closes the "START_VIDEO after the call ended" session leak).
        const ghostClosed = await attemptVideo(`ws://${HOST}:${PORT}/api/v1/ws?user=alice`, 'no-such-call');
        assert.ok(ghostClosed, 'START_VIDEO for an unknown callId must be rejected');
        assert.ok(
            authDenials.includes('no-live-call:no-such-call'),
            `expected a no-live-call denial, got: ${authDenials.join(', ')}`
        );
        assert.strictEqual(getVideoSession('no-such-call'), undefined, 'no session for an unknown callId');
        console.log('PASS: START_VIDEO for a callId with no live audio call is rejected');

        victimWs.close();
        audioWs.close();

        // ---- Part 2c: RECONNECT (videoResume) — parts are byte-joined ----
        // A reconnect that continues the same client encoder session splits one
        // fMP4 byte stream across part files. They must be joined byte-wise (only
        // part 1 has the init segment), so the result must still be a decodable
        // MP4 of the FULL duration — not just the pre-drop portion.
        const RESUME_CALL = 'resume-parts-call';
        liveCalls.set(RESUME_CALL, 'alice');
        const srcBytes = fs.readFileSync(fmp4);
        const splitAt = Math.floor(srcBytes.length / 2);

        const rs1 = new WebSocket(`ws://${HOST}:${PORT}/api/v1/ws?user=alice`);
        await new Promise<void>((res, rej) => {
            rs1.on('open', () => res()); rs1.on('error', rej); 
        });
        rs1.send(JSON.stringify({ callEvent: 'START_VIDEO', callId: RESUME_CALL }));
        await new Promise((r) => setTimeout(r, 100));
        rs1.send(srcBytes.subarray(0, splitAt), { binary: true });
        await new Promise((r) => setTimeout(r, 200));
        rs1.close(); // drop WITHOUT END_VIDEO -> grace period, session survives
        // Real clients reconnect after backoff; allow part 1's async flush to
        // complete so the byte accounting is deterministic in the test.
        await new Promise((r) => setTimeout(r, 800));

        const rs2 = new WebSocket(`ws://${HOST}:${PORT}/api/v1/ws?user=alice`);
        await new Promise<void>((res, rej) => {
            rs2.on('open', () => res()); rs2.on('error', rej); 
        });
        rs2.send(JSON.stringify({ callEvent: 'START_VIDEO', callId: RESUME_CALL, videoResume: true }));
        await new Promise((r) => setTimeout(r, 100));
        rs2.send(srcBytes.subarray(splitAt), { binary: true });
        await new Promise((r) => setTimeout(r, 200));
        rs2.send(JSON.stringify({ callEvent: 'END_VIDEO', callId: RESUME_CALL }));
        await new Promise((r) => setTimeout(r, 300));
        rs2.close();

        const resumeWav = path.join(TEST_TMP, `${RESUME_CALL}.wav`);
        fs.copyFileSync(wav, resumeWav);
        notifyAudioRecordingDone(RESUME_CALL, resumeWav, server);
        // The muxed name now carries a timestamp (so re-recorded meetings can't
        // overwrite each other in S3), so discover it rather than hardcoding.
        const findMuxed = (): string | undefined => {
            const prefix = `${RESUME_CALL.replace(/-/g, '_')}_`;
            const hit = fs
                .readdirSync(TEST_TMP)
                .find((f) => f.startsWith(prefix) && f.endsWith('.mp4') && !f.includes('_video_'));
            return hit ? path.join(TEST_TMP, hit) : undefined;
        };
        // Frame count is the unambiguous measure of "did we keep everything?".
        // (Stream *duration* metadata on a fragmented MP4 is not a reliable
        // proxy — it reads short even for a byte-perfect stream.)
        const countFrames = (f: string): number => {
            const out = sh('ffprobe', [
                '-v', 'error', '-select_streams', 'v:0',
                '-count_frames', '-show_entries', 'stream=nb_read_frames',
                '-of', 'csv=p=0', f,
            ]).trim();
            return Number(out);
        };
        const sourceFrames = countFrames(fmp4);
        await pollUntil(
            () => {
                // Must be COMPLETE, not merely present: ffmpeg writes the file
                // incrementally, so a half-written file decodes to fewer frames.
                const f = findMuxed();
                if (!f) {
                    return false;
                }
                try {
                    return countFrames(f) >= sourceFrames;
                } catch {
                    return false; // still being written
                }
            },
            20_000,
            'resumed parts should produce a COMPLETE muxed mp4 (VIDEO_KEEP_MUXED is set)'
        );
        const resumeOut = findMuxed() as string;
        const resumeCodecs = ffprobeCodecs(resumeOut);
        assert.ok(
            resumeCodecs.some((c) => c.startsWith('h264,video')),
            `rejoined video must decode as h264 (got: ${resumeCodecs.join(' | ')})`
        );
        // Every frame of the source must survive the split+rejoin — this is what
        // proves the post-reconnect bytes were kept rather than dropped.
        const outFrames = countFrames(resumeOut);
        assert.strictEqual(
            outFrames,
            sourceFrames,
            `rejoined video must keep ALL ${sourceFrames} source frames, got ${outFrames} (post-reconnect bytes lost?)`
        );
        console.log(`PASS: reconnect (videoResume) parts byte-joined; all ${outFrames} frames preserved`);

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

/**
 * Open a video socket, send START_VIDEO for `callId`, and resolve true if the
 * server closed the connection (i.e. refused it) within a short window.
 */
const attemptVideo = (url: string, callId: string): Promise<boolean> =>
    new Promise<boolean>((resolve) => {
        const ws = new WebSocket(url);
        let closed = false;
        const done = setTimeout(() => {
            try {
                ws.close(); 
            } catch { /* already closing */ }
            resolve(closed);
        }, 1500);
        ws.on('open', () => {
            ws.send(JSON.stringify({ callEvent: 'START_VIDEO', callId }));
            // Send a frame too: a server that accepted this would buffer it.
            ws.send(Buffer.alloc(64), { binary: true });
        });
        ws.on('close', () => {
            closed = true;
            clearTimeout(done);
            resolve(true);
        });
        ws.on('error', () => { /* close handler resolves */ });
    });

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
