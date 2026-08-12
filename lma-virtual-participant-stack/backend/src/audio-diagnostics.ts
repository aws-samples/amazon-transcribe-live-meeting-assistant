/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */

/**
 * READ-ONLY diagnostics for the voice assistant audio path (GitHub #543).
 *
 * The assistant's audio crackles on Teams but not Zoom. Three fixes have failed,
 * each reasoned from a mechanism rather than a measurement:
 *
 *   #538 pinned the PulseAudio sinks to 16 kHz mono — real inefficiency, but Zoom
 *        sounded fine on identical sinks, so resampling was never the cause.
 *   #544 cut the avatar's per-frame canvas work and widened the audio buffers —
 *        Simli reconnects went 2 -> 0, but the rescale path was not even active in
 *        the failing session and it still crackled.
 *   #545 forced echoCancellation/noiseSuppression/autoGainControl off — this BROKE
 *        Teams transcription outright and was reverted.
 *
 * What IS established, by measuring the container's own recording of
 * `agent_output`: the signal leaving the container is pristine on both platforms
 * (0 sample-level discontinuities above the click threshold, 0 clipped samples,
 * peak 18245/32767). So the damage happens after the audio leaves PulseAudio, in
 * Chromium's capture / processing / Opus encode — a segment never instrumented.
 *
 * WHY THIS DOES NOT LOG FROM THE PAGE
 * -----------------------------------
 * A first version of this file logged from inside the page with console.log and
 * produced NOTHING across three live sessions. Counting the captured console lines
 * by type explains why:
 *
 *     Browser log:       0
 *     Browser warning:   6
 *     Browser error:     4
 *
 * `page.on('console')` in index.ts is working, but plain `console.log` from the
 * page never arrives under cloakbrowser — only warnings and errors do. The Simli
 * override's own init-script logs were silent for the same reason, which is why
 * the audio processing module went unsuspected for so long.
 *
 * So the page side only ACCUMULATES observations onto `window.__lmaAudio`, and
 * Node drains and prints them on an interval with its own console.log, which
 * demonstrably reaches CloudWatch. No dependency on console forwarding.
 *
 * EVERYTHING HERE IS OBSERVATIONAL. Nothing mutates constraints, tracks or
 * streams; getUserMedia returns the original stream object unchanged. #545 proved
 * that touching this path can silently kill transcription.
 */
import type { Page } from 'playwright-core';

/** How often Node drains the page-side buffer and samples sender stats. */
export const STATS_INTERVAL_MS = 5000;

/** Opus at the standard 20 ms frame duration. */
export const EXPECTED_PACKETS_PER_SECOND = 50;

/** One sampled window of outbound audio sender stats. */
export interface OutboundAudioSample {
    packetsPerSecond: number;
    sendDelayPerPacketMs?: number;
}

/**
 * Classify a sample against the expected Opus cadence. Pure, so the thresholds
 * are testable without a browser.
 */
export function classifyOutboundAudio(sample: OutboundAudioSample): {
    healthy: boolean;
    reason: string;
} {
    const pps = sample.packetsPerSecond;
    if (pps <= 0) return { healthy: false, reason: 'no audio packets being sent' };
    // +/-20%: the sampling window is not frame-aligned, so a healthy sender drifts.
    if (pps < EXPECTED_PACKETS_PER_SECOND * 0.8) {
        return {
            healthy: false,
            reason: `cadence ${pps.toFixed(1)}/s is below the expected ~${EXPECTED_PACKETS_PER_SECOND}/s — encoder or capture starving`,
        };
    }
    if (pps > EXPECTED_PACKETS_PER_SECOND * 1.2) {
        return {
            healthy: false,
            reason: `cadence ${pps.toFixed(1)}/s exceeds the expected ~${EXPECTED_PACKETS_PER_SECOND}/s — bursting after a stall`,
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
 * Install the page-side collectors. Must run BEFORE navigation: addInitScript only
 * applies to documents created afterwards — the reason the Simli override never
 * took effect on Teams.
 */
export async function installAudioDiagnostics(page: Page): Promise<void> {
    await page.addInitScript(() => {
        const w = window as unknown as Record<string, unknown>;
        // Buffer of observations for Node to drain. Never logged from here: page
        // console.log does not reach CloudWatch under cloakbrowser.
        if (!w.__lmaAudio) w.__lmaAudio = [];
        const push = (msg: string) => {
            const buf = w.__lmaAudio as string[];
            // Bounded, so a long meeting cannot grow this without limit.
            if (buf.length < 500) buf.push(msg);
        };

        // ---- observe getUserMedia (no mutation) ----
        const md = navigator.mediaDevices;
        if (md && md.getUserMedia) {
            const original = md.getUserMedia.bind(md);
            md.getUserMedia = async function (constraints?: MediaStreamConstraints) {
                if (constraints && constraints.audio) {
                    try {
                        push(`gUM requested audio=${JSON.stringify(constraints.audio)}`);
                    } catch {
                        /* never affect capture */
                    }
                }
                const stream = await original(constraints);
                try {
                    for (const t of stream.getAudioTracks()) {
                        push(`track settings ${JSON.stringify(t.getSettings ? t.getSettings() : {})}`);
                    }
                } catch {
                    /* ignore */
                }
                return stream;
            };
        }

        // ---- collect outbound audio senders for Node to poll ----
        const NativePC = window.RTCPeerConnection;
        if (NativePC) {
            const pcs: RTCPeerConnection[] = [];
            w.__lmaPCs = pcs;
            const Wrapped = function (this: unknown, ...args: unknown[]) {
                // eslint-disable-next-line @typescript-eslint/no-explicit-any
                const pc = new (NativePC as any)(...args);
                if (pcs.length < 32) pcs.push(pc);
                return pc;
            } as unknown as typeof RTCPeerConnection;
            Wrapped.prototype = NativePC.prototype;
            window.RTCPeerConnection = Wrapped;
            push('RTCPeerConnection instrumented');
        }
        push(`diagnostics installed in frame ${location.href.slice(0, 120)}`);
    });
}

/* c8 ignore start - needs a live page; exercised by the container e2e test */

/**
 * Start Node-side draining. Logs with Node's console.log, which reaches
 * CloudWatch — unlike page console.log (see the module docstring).
 *
 * Returns a stop function so the caller can clear the timer on teardown.
 */
export function startAudioDiagnosticsPolling(page: Page): () => void {
    const prev = new Map<string, { packets: number; delay: number; ts: number }>();

    const timer = setInterval(() => {
        void (async () => {
            try {
                // 1. Drain page-side observations.
                const lines: string[] = await page.evaluate(() => {
                    const w = window as unknown as Record<string, unknown>;
                    const buf = (w.__lmaAudio as string[]) || [];
                    w.__lmaAudio = [];
                    return buf;
                });
                for (const line of lines) console.log(`[LMA-Audio] ${line}`);

                // 2. Sample outbound audio sender stats from every peer connection.
                const samples: Array<{ id: string; packets: number; bytes: number; delay: number; ts: number }> =
                    await page.evaluate(async () => {
                        const w = window as unknown as Record<string, unknown>;
                        const pcs = (w.__lmaPCs as RTCPeerConnection[]) || [];
                        const out: Array<{ id: string; packets: number; bytes: number; delay: number; ts: number }> = [];
                        for (const pc of pcs) {
                            if (pc.connectionState === 'closed') continue;
                            try {
                                const report = await pc.getStats();
                                report.forEach((s: Record<string, unknown>) => {
                                    if (s.type !== 'outbound-rtp' || s.kind !== 'audio') return;
                                    out.push({
                                        id: String(s.id),
                                        packets: Number(s.packetsSent ?? 0),
                                        bytes: Number(s.bytesSent ?? 0),
                                        delay: Number(s.totalPacketSendDelay ?? 0),
                                        ts: Number(s.timestamp ?? 0),
                                    });
                                });
                            } catch {
                                /* ignore a dead pc */
                            }
                        }
                        return out;
                    });

                for (const s of samples) {
                    const last = prev.get(s.id);
                    prev.set(s.id, { packets: s.packets, delay: s.delay, ts: s.ts });
                    if (!last || s.ts <= last.ts) continue;
                    const secs = (s.ts - last.ts) / 1000;
                    const dPackets = s.packets - last.packets;
                    const pps = dPackets / secs;
                    const perPacketMs = dPackets > 0 ? ((s.delay - last.delay) / dPackets) * 1000 : 0;
                    const verdict = classifyOutboundAudio({
                        packetsPerSecond: pps,
                        sendDelayPerPacketMs: perPacketMs,
                    });
                    console.log(
                        `[LMA-Audio] outbound pps=${pps.toFixed(1)} ` +
                            `sendDelayPerPacketMs=${perPacketMs.toFixed(2)} ` +
                            `${verdict.healthy ? 'OK' : 'PROBLEM'}: ${verdict.reason}`,
                    );
                }
            } catch {
                // Page closed / navigated / CDP hiccup. Diagnostics must never
                // affect the meeting, so swallow and try again next tick.
            }
        })();
    }, STATS_INTERVAL_MS);

    return () => clearInterval(timer);
}

/* c8 ignore stop */
