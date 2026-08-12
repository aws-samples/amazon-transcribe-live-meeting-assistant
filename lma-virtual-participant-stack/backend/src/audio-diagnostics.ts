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
 *        Teams transcription outright and was reverted. The first live capture from
 *        this module then showed WHY it could never have helped: Teams already
 *        requests all three off, and the track confirms them off.
 *
 * What IS established: the container's own recording of `agent_output` is pristine
 * on both platforms (0 sample-level discontinuities above the click threshold, 0
 * clipped samples, peak 18245/32767). The damage happens after the audio leaves
 * PulseAudio — in Chromium's capture / encode / send, or on the wire.
 *
 * WHAT THE FIRST VERSION OF THIS FILE GOT WRONG
 * ---------------------------------------------
 * It sampled `packetsSent` every 5s and compared the rate against Opus' 50/s. On a
 * live Teams call that produced a wall of PROBLEM verdicts at ~4.8 packets/s during
 * silence and ~25/s around speech. Both were artefacts:
 *
 *   - 4.8/s is DTX comfort noise. A sender that is deliberately silent is healthy,
 *     so flagging it buried any real signal in false alarms.
 *   - ~25/s came from averaging a ~2.5s utterance over a 5s window. A perfectly
 *     healthy 50/s sender that speaks for half the window reads as exactly 25/s.
 *     The metric could not distinguish "half the frames dropped" from "spoke half
 *     the time", which is precisely the question being asked.
 *
 * So packet rate alone cannot answer this. This version measures quantities that
 * are independent of how much the agent happened to say:
 *
 *   captureRatio  ΔtotalSamplesDuration / Δwall-clock. Seconds of audio Chromium
 *                 actually captured per second of real time. ~1.0 on a healthy
 *                 path REGARDLESS of silence or speech; below that means capture is
 *                 losing audio before the encoder ever sees it. This is the direct
 *                 test of the starvation theory.
 *   energyDelta   ΔtotalAudioEnergy, used only to tell speech windows from silent
 *                 ones so cadence is judged when it is meaningful.
 *   codec/bitrate what Teams actually negotiated. A low negotiated Opus bitrate, a
 *                 narrowband codec, or a bandwidth estimator collapsing the target
 *                 all sound exactly like crackle, and Teams and Zoom negotiate
 *                 independently — an obvious candidate the old metric was blind to.
 *   remote loss   packetsLost / jitter as reported BY THE FAR END over RTCP. This
 *                 splits "we sent bad audio" from "the network broke good audio",
 *                 which no sender-side counter can do.
 *
 * EVERYTHING HERE IS OBSERVATIONAL. Nothing mutates constraints, tracks or
 * streams; getUserMedia returns the original stream object unchanged. #545 proved
 * that touching this path can silently kill transcription.
 *
 * WHY THE PAGE DOES NOT LOG
 * -------------------------
 * An earlier version logged from inside the page and produced nothing across three
 * live sessions. Counting captured console lines by type explains it:
 *
 *     Browser log: 0    Browser warning: 6    Browser error: 4
 *
 * `page.on('console')` works, but plain `console.log` from the page never arrives
 * under cloakbrowser. So the page side only ACCUMULATES onto `window.__lmaAudio`
 * and Node drains it with its own console.log, which demonstrably reaches
 * CloudWatch.
 */
import type { Page } from 'playwright-core';

/**
 * Sampling period. 1s so a single utterance spans several windows — the 5s window
 * used first could not resolve speech from silence, which is what made its packet
 * rates meaningless.
 */
export const STATS_INTERVAL_MS = 1000;

/** Opus at the standard 20 ms frame duration. */
export const EXPECTED_PACKETS_PER_SECOND = 50;

/**
 * ΔtotalAudioEnergy above which a window counts as containing speech. Energy is
 * the sum of squared sample amplitudes over duration, so silence is not exactly
 * zero — the null sink's own noise floor lands well below this.
 */
export const SPEECH_ENERGY_THRESHOLD = 1e-4;

/** Seconds of captured audio per second of wall clock below which capture is losing frames. */
export const MIN_CAPTURE_RATIO = 0.95;

/**
 * Longest window over which packet cadence still means anything. Beyond this, a
 * window mixes speech and silence and the average is uninterpretable — the exact
 * mistake that made the first live capture read as 50% frame loss when it was a
 * 2.5s utterance in a 5s window.
 */
export const MAX_CADENCE_WINDOW_SECONDS = 2;

/** One sampled window of outbound audio, already differenced against the previous. */
export interface AudioWindow {
    /** Wall-clock length of the window, seconds. */
    seconds: number;
    /** Packets sent during the window. */
    packets: number;
    /** ΔtotalSamplesDuration / seconds, if the media-source stat was available. */
    captureRatio?: number;
    /** ΔtotalAudioEnergy, used to detect whether the agent was speaking. */
    energyDelta?: number;
    /** Mean added latency per packet, milliseconds. */
    sendDelayPerPacketMs?: number;
    /** Packets the FAR END reported lost during the window. */
    remotePacketsLost?: number;
}

export type AudioState = 'idle' | 'ok' | 'problem';

/**
 * Classify one window. Pure, so the thresholds are testable without a browser —
 * and so the DTX false-alarm class that made the first version unusable stays
 * fixed.
 */
export function classifyAudioWindow(w: AudioWindow): { state: AudioState; reason: string } {
    const pps = w.seconds > 0 ? w.packets / w.seconds : 0;

    // Capture continuity first: it is independent of speech, of DTX and of the
    // sampling window, so it is the only metric here that can stand alone.
    if (w.captureRatio !== undefined && w.captureRatio < MIN_CAPTURE_RATIO) {
        return {
            state: 'problem',
            reason:
                `capture starving: ${w.captureRatio.toFixed(3)}s of audio captured per second of wall clock ` +
                `(need >= ${MIN_CAPTURE_RATIO}) — frames lost before the encoder`,
        };
    }
    if ((w.remotePacketsLost ?? 0) > 0) {
        return {
            state: 'problem',
            reason: `far end reported ${w.remotePacketsLost} lost packet(s) — damage on the wire, not in capture`,
        };
    }
    if ((w.sendDelayPerPacketMs ?? 0) > 20) {
        return {
            state: 'problem',
            reason: `send delay ${w.sendDelayPerPacketMs!.toFixed(1)}ms/packet indicates queueing`,
        };
    }

    // Silent windows are healthy: a DTX sender emits ~5 packets/s of comfort noise
    // by design. Judging cadence here is what produced the earlier false alarms.
    const speaking = (w.energyDelta ?? 0) > SPEECH_ENERGY_THRESHOLD;
    if (!speaking) {
        return { state: 'idle', reason: `silent (${pps.toFixed(1)} pkt/s comfort noise)` };
    }

    if (pps <= 0) return { state: 'problem', reason: 'audio present but no packets sent' };
    // Only a LOW cadence during speech is meaningful, and only over a window short
    // enough that the whole window WAS speech. A high cadence is expected: the
    // window boundary is not frame-aligned and a DTX sender resumes mid-window.
    if (w.seconds <= MAX_CADENCE_WINDOW_SECONDS && pps < EXPECTED_PACKETS_PER_SECOND * 0.7) {
        return {
            state: 'problem',
            reason: `speaking but only ${pps.toFixed(1)} pkt/s vs ~${EXPECTED_PACKETS_PER_SECOND}/s expected`,
        };
    }
    return { state: 'ok', reason: `speaking, ${pps.toFixed(1)} pkt/s` };
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

/** Raw per-sender snapshot pulled out of getStats(). */
interface SenderSnapshot {
    id: string;
    packets: number;
    bytes: number;
    delay: number;
    ts: number;
    samplesDuration?: number;
    energy?: number;
    remoteLost?: number;
    jitterMs?: number;
    targetBitrate?: number;
    codec?: string;
}

/**
 * Start Node-side draining. Logs with Node's console.log, which reaches
 * CloudWatch — unlike page console.log (see the module docstring).
 *
 * Returns a stop function so the caller can clear the timer on teardown.
 */
export function startAudioDiagnosticsPolling(page: Page): () => void {
    const prev = new Map<string, SenderSnapshot>();
    /** Codecs already reported, so the negotiated parameters are logged once each. */
    const reportedCodecs = new Set<string>();
    /** Consecutive problem windows per sender, so a burst collapses into one line. */
    const problemRun = new Map<string, number>();
    let ticks = 0;

    const timer = setInterval(() => {
        void (async () => {
            try {
                ticks += 1;

                // 1. Drain page-side observations.
                const lines: string[] = await page.evaluate(() => {
                    const w = window as unknown as Record<string, unknown>;
                    const buf = (w.__lmaAudio as string[]) || [];
                    w.__lmaAudio = [];
                    return buf;
                });
                for (const line of lines) console.log(`[LMA-Audio] ${line}`);

                // 2. Snapshot every outbound audio sender, joined to its media
                //    source, negotiated codec and the far end's RTCP report.
                const snaps: SenderSnapshot[] = await page.evaluate(async () => {
                    const w = window as unknown as Record<string, unknown>;
                    const pcs = (w.__lmaPCs as RTCPeerConnection[]) || [];
                    const out: SenderSnapshot[] = [];
                    for (const pc of pcs) {
                        if (pc.connectionState === 'closed') continue;
                        try {
                            const report = await pc.getStats();
                            const byId = new Map<string, Record<string, unknown>>();
                            report.forEach((s: Record<string, unknown>) => byId.set(String(s.id), s));
                            report.forEach((s: Record<string, unknown>) => {
                                if (s.type !== 'outbound-rtp' || s.kind !== 'audio') return;
                                const src = s.mediaSourceId ? byId.get(String(s.mediaSourceId)) : undefined;
                                const codec = s.codecId ? byId.get(String(s.codecId)) : undefined;
                                // The far end's view arrives as a remote-inbound-rtp
                                // whose localId points back at this sender.
                                let remote: Record<string, unknown> | undefined;
                                byId.forEach((cand) => {
                                    if (cand.type === 'remote-inbound-rtp' && String(cand.localId) === String(s.id)) {
                                        remote = cand;
                                    }
                                });
                                out.push({
                                    id: String(s.id),
                                    packets: Number(s.packetsSent ?? 0),
                                    bytes: Number(s.bytesSent ?? 0),
                                    delay: Number(s.totalPacketSendDelay ?? 0),
                                    ts: Number(s.timestamp ?? 0),
                                    samplesDuration: src ? Number(src.totalSamplesDuration ?? NaN) : undefined,
                                    energy: src ? Number(src.totalAudioEnergy ?? NaN) : undefined,
                                    remoteLost: remote ? Number(remote.packetsLost ?? NaN) : undefined,
                                    jitterMs: remote ? Number(remote.jitter ?? NaN) * 1000 : undefined,
                                    targetBitrate: Number(s.targetBitrate ?? NaN),
                                    codec: codec
                                        ? `${codec.mimeType} ${codec.clockRate}Hz/${codec.channels ?? 1}ch ` +
                                          `fmtp=${codec.sdpFmtpLine ?? '-'}`
                                        : undefined,
                                });
                            });
                        } catch {
                            /* ignore a dead pc */
                        }
                    }
                    return out;
                });

                for (const s of snaps) {
                    // What Teams negotiated is a prime suspect and is static, so log
                    // it once rather than every second.
                    if (s.codec && !reportedCodecs.has(s.codec)) {
                        reportedCodecs.add(s.codec);
                        console.log(`[LMA-Audio] negotiated ${s.codec}`);
                    }

                    const last = prev.get(s.id);
                    prev.set(s.id, s);
                    if (!last || s.ts <= last.ts) continue;
                    const seconds = (s.ts - last.ts) / 1000;
                    const dPackets = s.packets - last.packets;
                    const finite = (n?: number) => (n !== undefined && Number.isFinite(n) ? n : undefined);
                    const dSamples =
                        finite(s.samplesDuration) !== undefined && finite(last.samplesDuration) !== undefined
                            ? s.samplesDuration! - last.samplesDuration!
                            : undefined;
                    const window: AudioWindow = {
                        seconds,
                        packets: dPackets,
                        captureRatio: dSamples !== undefined ? dSamples / seconds : undefined,
                        energyDelta:
                            finite(s.energy) !== undefined && finite(last.energy) !== undefined
                                ? s.energy! - last.energy!
                                : undefined,
                        sendDelayPerPacketMs:
                            dPackets > 0 ? ((s.delay - last.delay) / dPackets) * 1000 : 0,
                        remotePacketsLost:
                            finite(s.remoteLost) !== undefined && finite(last.remoteLost) !== undefined
                                ? s.remoteLost! - last.remoteLost!
                                : undefined,
                    };
                    const verdict = classifyAudioWindow(window);

                    // Log every problem and every speech window; summarise idle
                    // periodically so the log shows liveness without a line a second
                    // for the whole meeting.
                    const run = verdict.state === 'problem' ? (problemRun.get(s.id) ?? 0) + 1 : 0;
                    problemRun.set(s.id, run);
                    const idleHeartbeat = verdict.state === 'idle' && ticks % 30 === 0;
                    // A sustained problem prints for the first 5s then every 10s.
                    const problemThrottled = verdict.state === 'problem' && run > 5 && run % 10 !== 0;
                    if (verdict.state === 'ok' || idleHeartbeat || (verdict.state === 'problem' && !problemThrottled)) {
                        const kbps = ((s.bytes - last.bytes) * 8) / 1000 / seconds;
                        console.log(
                            `[LMA-Audio] ${verdict.state.toUpperCase()} ${verdict.reason} | ` +
                                `captureRatio=${window.captureRatio?.toFixed(3) ?? 'n/a'} ` +
                                `energyDelta=${window.energyDelta?.toExponential(2) ?? 'n/a'} ` +
                                `kbps=${kbps.toFixed(1)} ` +
                                `targetKbps=${Number.isFinite(s.targetBitrate!) ? (s.targetBitrate! / 1000).toFixed(1) : 'n/a'} ` +
                                `remoteLost=${window.remotePacketsLost ?? 'n/a'} ` +
                                `jitterMs=${Number.isFinite(s.jitterMs!) ? s.jitterMs!.toFixed(1) : 'n/a'}` +
                                (run > 5 ? ` (x${run} consecutive)` : ''),
                        );
                    }
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
