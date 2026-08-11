/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */

/**
 * Force the meeting client's microphone capture to skip browser audio processing.
 *
 * The VP's "microphone" is `agent_mic` — a PulseAudio remap-source of
 * `agent_output`, which carries nothing but Nova Sonic's synthesised speech. It is
 * a clean, single-source, 16 kHz mono signal with no room, no echo path and no
 * background noise.
 *
 * Chromium nonetheless applies its full WebRTC audio processing module to it,
 * because meeting clients request `echoCancellation` / `noiseSuppression` /
 * `autoGainControl` by default (and Teams requests them aggressively). Running
 * noise suppression and AGC over synthetic TTS is both pointless and harmful: the
 * suppressor mistakes steady synthetic speech for noise and gates it, and the AGC
 * pumps the level. The audible result is crackly, uneven speech (GitHub #543).
 *
 * Measured evidence that the problem is here and not upstream: the container's own
 * recording of `agent_output` is pristine on both platforms — 0 sample-level
 * discontinuities, 0 clipped samples, peak 18245/32767 — yet Teams sounds crackly
 * and Zoom does not. So the signal leaving the container is clean and the damage
 * happens between `agent_mic` and the meeting's encoder, which is exactly where
 * the APM sits. Teams being worse is consistent with it being the heavier client
 * and requesting more processing.
 *
 * Disabling the APM also removes real per-frame CPU work, which matters on the
 * MicroVM launch type: it is ARM64 with 2 vCPU, whereas the ECS task definition
 * runs 2 vCPU on x86_64 — and the crackle was not present on Fargate.
 *
 * This is installed independently of the Simli avatar override. That override is
 * only injected once the avatar is ready, by which point the meeting document
 * already exists — `addInitScript` only applies to documents created afterwards,
 * so on Teams it never ran at all (zero `[LMA-Simli]` browser lines in a live
 * session). Audio must not depend on that timing.
 */
import type { Page } from 'playwright-core';

/** Audio processing that must be off for a synthetic, single-source mic. */
export const FORCED_AUDIO_CONSTRAINTS = {
    echoCancellation: false,
    noiseSuppression: false,
    autoGainControl: false,
} as const;

/**
 * Merge the forced flags into whatever the meeting client asked for.
 *
 * Pure and exported so the merge semantics are unit-testable without a browser.
 *
 * `audio: true` is widened to an object so the flags can be attached. `audio:
 * false`/absent is left alone — the caller genuinely wants no audio. Any other
 * caller-supplied fields (deviceId, sampleRate, channelCount) are preserved: the
 * goal is to disable processing, not to override device selection.
 */
export function applyForcedAudioConstraints(
    constraints: MediaStreamConstraints | undefined,
): MediaStreamConstraints | undefined {
    if (!constraints || !constraints.audio) return constraints;
    const existing = constraints.audio === true ? {} : { ...(constraints.audio as object) };
    return { ...constraints, audio: { ...existing, ...FORCED_AUDIO_CONSTRAINTS } };
}

/**
 * Install the override in the page and every frame it later creates.
 *
 * MUST be called before the meeting URL is navigated to: `addInitScript` only
 * runs in documents created after it is registered.
 *
 * Also logs the constraints each caller requests. That is deliberately verbose —
 * these constraints were previously invisible, which is why the APM was not
 * suspected for two rounds of misdiagnosis.
 */
export async function installMicConstraintOverride(page: Page): Promise<void> {
    await page.addInitScript((forced: Record<string, boolean>) => {
        const md = navigator.mediaDevices;
        if (!md || !md.getUserMedia) return;
        const original = md.getUserMedia.bind(md);
        md.getUserMedia = function (constraints?: MediaStreamConstraints) {
            try {
                if (constraints && constraints.audio) {
                    const before = JSON.stringify(constraints.audio);
                    const existing = constraints.audio === true ? {} : { ...(constraints.audio as object) };
                    const merged = { ...existing, ...forced };
                    // eslint-disable-next-line no-param-reassign
                    constraints = { ...constraints, audio: merged };
                    console.log(
                        `[LMA-Mic] audio constraints: requested=${before} -> forced=${JSON.stringify(merged)}`,
                    );
                }
            } catch (e) {
                // Never let the override break capture: a meeting with processed
                // audio is far better than a meeting with no audio at all.
                console.log(`[LMA-Mic] constraint override skipped: ${(e as Error)?.message}`);
            }
            return original(constraints);
        };
        console.log(`[LMA-Mic] mic constraint override installed in frame: ${location.href}`);
    }, FORCED_AUDIO_CONSTRAINTS as unknown as Record<string, boolean>);
}
