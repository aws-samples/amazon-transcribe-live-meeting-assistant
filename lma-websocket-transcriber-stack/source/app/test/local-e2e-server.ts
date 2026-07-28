/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 *
 * Local E2E harness for the desktop-video lane: a minimal websocket server
 * that mirrors index.ts's protocol handling (START/PCM/END on the audio
 * socket; START_VIDEO/fMP4/END_VIDEO on the video socket, via the REAL
 * src/videorecording.ts module) WITHOUT requiring Cognito or AWS.
 *
 * Purpose: point a real native client (macOS/Windows app with --video 1) at
 * ws://localhost:8082/api/v1/ws and verify the full client->server flow
 * produces a muxed MP4. S3 upload/KDS fail gracefully offline; the harness
 * keeps the muxed file for inspection by setting LOCAL_KEEP_MUXED=1 handling
 * in this file (it copies the mux output before videorecording.ts cleanup).
 *
 * Run:  LOCAL_TEMP_DIR=/tmp/lma-e2e/ npx ts-node test/local-e2e-server.ts
 * Then: LMAAudioClient --endpoint ws://127.0.0.1:8082/api/v1/ws \
 *         --token dummy --call-id "local video test" --video 1
 * Stop the client (Ctrl-C); the harness logs the muxed MP4 path + ffprobe.
 */
import fs from 'fs';
import path from 'path';
import { execFileSync } from 'child_process';
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
} from '../src/videorecording';
import { CallMetaData } from '../src/calleventdata/eventtypes';
import { createWavHeader } from '../src/utils/wav';

const PORT = parseInt(process.env['SMOKE_PORT'] ?? '8082', 10);
const TMP = process.env['LOCAL_TEMP_DIR'] || '/tmp/lma-e2e/';
fs.mkdirSync(TMP, { recursive: true });

type AudioSession = {
    callId: string;
    samplingRate: number;
    rawPath: string;
    stream: fs.WriteStream;
    bytes: number;
};

const server = fastify({ logger: { level: 'info' } });
server.register(websocket);

const audioSockets = new Map<WebSocket, AudioSession>();
const videoSockets = new Map<WebSocket, VideoSession>();

const posixify = (s: string) => s.replace(/[^a-zA-Z0-9_.]/g, '_');

const endAudio = async (sess: AudioSession) => {
    await new Promise<void>((r) => sess.stream.end(() => r()));
    // Raw PCM -> WAV (header prepend), same as index.ts endCall.
    const wavPath = path.resolve(TMP, `${posixify(sess.callId)}.wav`);
    const header = createWavHeader(sess.samplingRate, sess.bytes);
    const out = fs.createWriteStream(wavPath);
    out.write(header);
    for await (const chunk of fs.createReadStream(sess.rawPath)) {
        out.write(chunk);
    }
    await new Promise<void>((r) => out.end(() => r()));
    server.log.info(`audio WAV ready: ${wavPath} (${sess.bytes} PCM bytes)`);
    const adopted = notifyAudioRecordingDone(sess.callId, wavPath, server);
    server.log.info(`video session adopted WAV: ${adopted}`);
};

server.after(() => {
    server.get('/api/v1/ws', { websocket: true }, (socket) => {
        socket.on('message', async (data: WebSocket.RawData, isBinary: boolean) => {
            if (isBinary) {
                const v = videoSockets.get(socket as WebSocket);
                if (v) {
                    writeVideoChunk(v, Buffer.from(data as Buffer), server);
                    return;
                }
                const a = audioSockets.get(socket as WebSocket);
                if (a) {
                    const buf = Buffer.from(data as Buffer);
                    a.stream.write(buf);
                    a.bytes += buf.length;
                }
                return;
            }
            const meta = JSON.parse(Buffer.from(data as Buffer).toString('utf8')) as CallMetaData;
            server.log.info(`text frame: ${meta.callEvent} [${meta.callId}]`);
            if (meta.callEvent === 'START') {
                const rawPath = path.resolve(TMP, `${posixify(meta.callId)}.raw`);
                audioSockets.set(socket as WebSocket, {
                    callId: meta.callId,
                    samplingRate: meta.samplingRate,
                    rawPath,
                    stream: fs.createWriteStream(rawPath),
                    bytes: 0,
                });
            } else if (meta.callEvent === 'END') {
                const a = audioSockets.get(socket as WebSocket);
                audioSockets.delete(socket as WebSocket);
                if (a) {
                    await endAudio(a);
                }
            } else if (meta.callEvent === 'START_VIDEO') {
                const session = startVideoSession(meta, server);
                videoSockets.set(socket as WebSocket, session);
            } else if (meta.callEvent === 'END_VIDEO') {
                const v = videoSockets.get(socket as WebSocket);
                videoSockets.delete(socket as WebSocket);
                if (v) {
                    await endVideoSession(v.callId, server);
                }
            }
        });
        socket.on('close', async () => {
            const v = videoSockets.get(socket as WebSocket);
            if (v) {
                videoSockets.delete(socket as WebSocket);
                videoSocketDropped(v.callId, server);
                return;
            }
            const a = audioSockets.get(socket as WebSocket);
            if (a) {
                audioSockets.delete(socket as WebSocket);
                await endAudio(a);
            }
        });
    });
});

// Watch for the muxed MP4 appearing in TMP (videorecording.ts writes it there
// before its S3 attempt) and snapshot it before cleanup deletes it.
const seen = new Set<string>();
setInterval(() => {
    for (const f of fs.readdirSync(TMP)) {
        if (!f.endsWith('.mp4') || f.includes('_video_') || f.startsWith('kept_')) {
            continue;
        }
        const full = path.join(TMP, f);
        if (seen.has(f)) {
            continue;
        }
        seen.add(f);
        const kept = path.join(TMP, `kept_${f}`);
        try {
            fs.copyFileSync(full, kept);
            const probe = execFileSync('ffprobe', [
                '-v', 'error',
                '-show_entries', 'stream=codec_name,codec_type',
                '-of', 'csv=p=0', kept,
            ]).toString().trim();
            server.log.info(`\n=== MUXED RECORDING KEPT: ${kept} ===\nstreams:\n${probe}\n===`);
        } catch {
            seen.delete(f); // mux still in progress; retry next tick
        }
    }
}, 500);

server.listen({ port: PORT, host: '127.0.0.1' }).then(() => {
    server.log.info(`local E2E server on ws://127.0.0.1:${PORT}/api/v1/ws (temp: ${TMP})`);
});
