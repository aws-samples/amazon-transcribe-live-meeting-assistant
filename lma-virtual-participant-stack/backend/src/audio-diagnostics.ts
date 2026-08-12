/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */

/**
 * READ-ONLY diagnostics for the voice assistant audio path (GitHub #543).
 *
 * The assistant's audio crackles on Teams but not Zoom. Three fixes have failed,
 * each reasoned from a plausible mechanism rather than a measurement:
 *
 *   1. #538 pinned the PulseAudio sinks to 16 kHz mono. Real inefficiency, but
 *      Zoom sounded fine on identical sinks, so resampling was never the cause.
 *   2. #544 cut the avatar's per-frame canvas work and widened the audio buffers.
 *      Simli reconnects went 2 -> 0, but the rescale path was not even active in
 *      the failing session and it still crackled.
 *   3. #545 forced echoCancellation/noiseSuppression/autoGainControl off. That
 *      BROKE Teams transcription outright and was reverted.
 *
 * What is actually known, from measuring the container's own recording of
 * `agent_output`: the signal leaving the container is pristine on BOTH platforms
 * (0 sample-level discontinuities above the click threshold, 0 clipped samples,
 * peak 18245/32767, 16 kHz mono). So the damage happens after the audio leaves
 * PulseAudio — somewhere in Chromium's capture, processing or Opus encode — and
 * that segment has never been instrumented.
 *
 * EVERYTHING HERE IS OBSERVATIONAL. Nothing mutates constraints, tracks or
 * streams. #545 proved that touching this path can silently kill transcription,
 * so the next change to it must be driven by numbers rather than by another
 * hypothesis.
 *
 * What to look for in the logs:
 *
 *   - `[LMA-Audio] gUM requested` — what the meeting client asks for. Never seen
 *     before; the Simli override that would have logged it is injected too late
 *     to run on Teams at all (zero browser-side lines in a live session).
 *   - `[LMA-Audio] track settings` — what processing is ACTUALLY in effect, which
 *     can differ from what was requested.
 *   - `[LMA-Audio] outbound` — the decisive one. Opus at 20 ms should send ~50
 *     packets/s at a steady cadence. Irregular `pps`, or growth in
 *     `sendDelayPerPacketMs`, localises the fault to Chromium's encode/send on
 *     this host. A clean, steady ~50 pps means the audio leaves correctly and our
 *     code is not the problem.
 */
import type { Page } from 'playwright-core';

/** How often to sample outbound audio stats. */
export const STATS_INTERVAL_MS = 5000;

/** Opus at the standard 20 ms frame duration. */
export const EXPECTED_PACKETS_PER_SECOND = 50;

/** One sampled window of outbound audio sender stats. */
export interface OutboundAudioSample {
    packetsPerSecond: number;
    audioLevel?: number;
    totalAudioEnergy?: number;
    /** Mean send delay added per packet in this window, in milliseconds. */
    sendDelayPerPacketMs?: number;
    bytesPerSecond?: number;
}

/**
 * Classify a sample against the expected Opus cadence.
 *
 * Pure so the thresholds are unit-testable without a browser. Deliberately
 * conservative: this only reports, and a wrong label here costs nothing, whereas
 * a wrong "fix" cost us transcription.
 */
export function classifyOutboundAudio(sample: OutboundAudioSample): {
    healthy: boolean;
    reason: string;
} {
    const pps = sample.packetsPerSecond;
    // Nothing being sent at all is a different failure from an irregular cadence.
    if (pps <= 0) return { healthy: false, reason: 'no audio packets being sent' };
    // Allow +/-20%: the sampling window is not frame-aligned, so a little drift is
    // expected even on a perfectly healthy sender.
    const low = EXPECTED_PACKETS_PER_SECOND * 0.8;
    const high = EXPECTED_PACKETS_PER_SECOND * 1.2;
    if (pps < low) {
        return {
            healthy: false,
            reason: `packet cadence ${pps.toFixed(1)}/s is below the expected ~${EXPECTED_PACKETS_PER_SECOND}/s — encoder or capture is starving`,
        };
    }
    if (pps > high) {
        return {
            healthy: false,
            reason: `packet cadence ${pps.toFixed(1)}/s exceeds the expected ~${EXPECTED_PACKETS_PER_SECOND}/s — bursting after a stall`,
        };
    }
    if ((sample.sendDelayPerPacketMs ?? 0) > 20) {
        return {
            healthy: false,
            reason: `send delay ${sample.sendDelayPerPacketMs!.toFixed(1)}ms/packet indicates queueing`,
        };
    }
    return { healthy: true, reason: `steady ${pps.toFixed(1)} packets/s` };
}

/**
 * Install the observers. Must be called BEFORE navigation: `addInitScript` only
 * runs in documents created after it is registered, which is precisely why the
 * Simli override never ran on Teams.
 */
export async function installAudioDiagnostics(page: Page): Promise<void> {
    await page.addInitScript(
        ({ intervalMs, expectedPps }: { intervalMs: number; expectedPps: number }) => {
            const md = navigator.mediaDevices;

            // ---- 1. Observe getUserMedia, without changing anything ----------
            if (md && md.getUserMedia) {
                const original = md.getUserMedia.bind(md);
                md.getUserMedia = async function (constraints?: MediaStreamConstraints) {
                    if (constraints && constraints.audio) {
                        try {
                            console.log(
                                `[LMA-Audio] gUM requested audio=${JSON.stringify(constraints.audio)}`,
                            );
                        } catch {
                            /* logging must never affect capture */
                        }
                    }
                    const stream = await original(constraints);
                    // Report what the browser actually applied — this can differ
                    // from the request, and it is the ground truth for whether
                    // echo cancellation / noise suppression / AGC are running.
                    try {
                        for (const track of stream.getAudioTracks()) {
                            const s = track.getSettings ? track.getSettings() : {};
                            console.log(`[LMA-Audio] track settings ${JSON.stringify(s)}`);
                        }
                    } catch {
                        /* ignore */
                    }
                    return stream;
                };
            }

            // ---- 2. Sample outbound audio sender stats -----------------------
            // Wrap RTCPeerConnection so every connection the meeting client makes
            // is sampled, without needing to know how it builds them.
            const NativePC = window.RTCPeerConnection;
            if (!NativePC) return;
            const pcs = new Set<RTCPeerConnection>();
            const Wrapped = function (this: unknown, ...args: unknown[]) {
                // eslint-disable-next-line @typescript-eslint/no-explicit-any
                const pc = new (NativePC as any)(...args);
                pcs.add(pc);
                return pc;
            } as unknown as typeof RTCPeerConnection;
            Wrapped.prototype = NativePC.prototype;
            window.RTCPeerConnection = Wrapped;

            const prev = new Map<string, { packets: number; bytes: number; delay: number; ts: number }>();
            setInterval(() => {
                pcs.forEach((pc) => {
                    if (pc.connectionState === 'closed') {
                        pcs.delete(pc);
                        return;
                    }
                    pc.getStats()
                        .then((report) => {
                            report.forEach((s: Record<string, unknown>) => {
                                if (s.type !== 'outbound-rtp' || s.kind !== 'audio') return;
                                const id = String(s.id);
                                const packets = Number(s.packetsSent ?? 0);
                                const bytes = Number(s.bytesSent ?? 0);
                                const delay = Number(s.totalPacketSendDelay ?? 0);
                                const ts = Number(s.timestamp ?? Date.now());
                                const last = prev.get(id);
                                prev.set(id, { packets, bytes, delay, ts });
                                if (!last || ts <= last.ts) return;
                                const secs = (ts - last.ts) / 1000;
                                const dPackets = packets - last.packets;
                                const pps = dPackets / secs;
                                const bps = (bytes - last.bytes) / secs;
                                const perPacketMs =
                                    dPackets > 0 ? ((delay - last.delay) / dPackets) * 1000 : 0;
                                const verdict =
                                    pps <= 0
                                        ? 'NO AUDIO SENT'
                                        : pps < expectedPps * 0.8 || pps > expectedPps * 1.2
                                          ? 'IRREGULAR'
                                          : 'steady';
                                console.log(
                                    `[LMA-Audio] outbound pps=${pps.toFixed(1)} (expect ~${expectedPps}) ` +
                                        `bytes/s=${bps.toFixed(0)} sendDelayPerPacketMs=${perPacketMs.toFixed(2)} ` +
                                        `verdict=${verdict}`,
                                );
                            });
                        })
                        .catch(() => undefined);
                });
            }, intervalMs);

            console.log('[LMA-Audio] diagnostics installed (observational only)');
        },
        { intervalMs: STATS_INTERVAL_MS, expectedPps: EXPECTED_PACKETS_PER_SECOND },
    );
}
