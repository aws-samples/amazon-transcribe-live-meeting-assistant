/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 *
 * Synthetic native-client stand-in for the local E2E harness: opens TWO
 * websockets to the harness (as the real macOS/Windows client does) and drives
 * the exact wire protocol with REAL media —
 *   - audio socket: START -> interleaved 16-bit stereo PCM frames -> END
 *   - video socket: START_VIDEO -> fragmented-MP4 segments -> END_VIDEO
 * so the whole server path (audio WAV + video mux -> single MP4) runs offline
 * without needing live screen/mic capture or Cognito.
 *
 * Run the harness first (test/local-e2e-server.ts), then:
 *   npx ts-node test/local-e2e-client.ts
 */
import fs from 'fs';
import os from 'os';
import path from 'path';
import { execFileSync } from 'child_process';
import WebSocket from 'ws';

const URL = process.env['E2E_URL'] || 'ws://127.0.0.1:8082/api/v1/ws';
const CALL_ID = process.env['E2E_CALL_ID'] || 'local video e2e - synthetic';
const SR = 48000;
const TMP = fs.mkdtempSync(path.join(os.tmpdir(), 'lma-e2e-client-'));

const sh = (cmd: string, args: string[]) => execFileSync(cmd, args, { stdio: ['ignore', 'ignore', 'inherit'] });
const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));

async function main(): Promise<void> {
    // Generate 4s of H.264 fragmented MP4 (video only) and 4s stereo PCM.
    const fmp4 = path.join(TMP, 'v.mp4');
    const pcm = path.join(TMP, 'a.pcm');
    sh('ffmpeg', [
        '-y', '-nostdin', '-loglevel', 'error',
        '-f', 'lavfi', '-i', 'testsrc=duration=4:size=640x360:rate=5',
        '-c:v', 'libx264', '-preset', 'ultrafast', '-pix_fmt', 'yuv420p', '-g', '10',
        '-movflags', 'frag_keyframe+empty_moov+default_base_moof', fmp4,
    ]);
    sh('ffmpeg', [
        '-y', '-nostdin', '-loglevel', 'error',
        '-f', 'lavfi', '-i', 'sine=frequency=440:duration=4',
        '-ac', '2', '-ar', `${SR}`, '-f', 's16le', pcm,
    ]);

    const audio = new WebSocket(URL);
    const video = new WebSocket(URL);
    const opened = (ws: WebSocket) => new Promise<void>((res, rej) => {
        ws.on('open', () => res()); ws.on('error', rej);
    });
    await Promise.all([opened(audio), opened(video)]);
    console.log('both sockets open');

    // START frames.
    audio.send(JSON.stringify({
        callId: CALL_ID, agentId: 'me@example.com', fromNumber: 'Other participants',
        toNumber: 'System', samplingRate: SR, callEvent: 'START',
    }));
    video.send(JSON.stringify({ callId: CALL_ID, callEvent: 'START_VIDEO', videoTimeOffsetMs: 120 }));
    await sleep(200);

    // Stream PCM in 100ms chunks and video in 32KiB chunks, interleaved.
    const pcmBytes = fs.readFileSync(pcm);
    const vidBytes = fs.readFileSync(fmp4);
    const pcmChunk = (SR / 10) * 2 * 2; // 100ms stereo 16-bit
    let vi = 0;
    const vStep = 32768;
    for (let pi = 0; pi < pcmBytes.length; pi += pcmChunk) {
        audio.send(pcmBytes.subarray(pi, pi + pcmChunk), { binary: true });
        // Roughly track video alongside audio.
        const vEnd = Math.min(vidBytes.length, vi + vStep * 2);
        for (; vi < vEnd; vi += vStep) {
            video.send(vidBytes.subarray(vi, vi + vStep), { binary: true });
        }
        await sleep(40);
    }
    // Flush any remaining video.
    for (; vi < vidBytes.length; vi += vStep) {
        video.send(vidBytes.subarray(vi, vi + vStep), { binary: true });
    }
    await sleep(300);

    // END frames: video first (client stops video before audio END), then audio.
    video.send(JSON.stringify({ callId: CALL_ID, callEvent: 'END_VIDEO' }));
    await sleep(300);
    audio.send(JSON.stringify({ callId: CALL_ID, callEvent: 'END', samplingRate: SR, shouldRecordCall: true }));
    await sleep(500);
    audio.close();
    video.close();
    console.log('sent END/END_VIDEO; check the harness log for the muxed MP4 path + ffprobe.');
    fs.rmSync(TMP, { recursive: true, force: true });
}

main().then(() => process.exit(0), (e) => {
    console.error(e); process.exit(1); 
});
