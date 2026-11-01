/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */
/* eslint-disable @typescript-eslint/no-explicit-any */

/**
 * Whether the meeting page is showing somebody's screen share.
 *
 * Key frames are only sampled while a share is on screen where the platform
 * gives a reliable signal for it, which keeps camera tiles and the gallery out
 * of the timeline. Where it gives none, the sampler runs all the time with
 * stricter thresholds (see keyframe-sampler.ts). The signal per platform:
 *
 *   - Zoom (web client, and the Meeting SDK client view, which renders the
 *     same interface): the share canvas container, `sharee-container`, and the
 *     small floating video window Zoom switches to while a share is shown,
 *     `single-suspension-container`. Both are read by zoom.ts already, for the
 *     in-meeting check and for the active speaker during a share.
 *   - Teams (web client): an element with `data-stream-type="ScreenSharing"`,
 *     which teams.ts reads for the active speaker during a share.
 *   - Teams (Azure Communication Services path): the call adapter's state,
 *     exposed by teams-sdk-page.ts as `__lmaScreenShareActive()`.
 *   - Webex and Amazon Chime: no signal; the probe is absent.
 *
 * A probe answers `null` when it cannot tell (the page is navigating, the
 * evaluate failed); the sampler keeps the previous answer in that case.
 */
import type { Page } from 'playwright-core';

export type ScreenShareProbe = (page: Page) => Promise<boolean | null>;

/** Selectors whose visible presence means a screen share is on screen, per platform. */
export const SHARE_SELECTORS: Record<string, readonly string[]> = {
    ZOOM: ['[class*="sharee-container"]', '[class*="single-suspension-container"]'],
    TEAMS: ['[data-stream-type="ScreenSharing"]'],
};

const selectorProbe =
    (selectors: readonly string[]): ScreenShareProbe =>
    async (page: Page) => {
        try {
            return await page.evaluate((sels: readonly string[]) => {
                for (const sel of sels) {
                    for (const el of Array.from(document.querySelectorAll(sel))) {
                        const r = (el as HTMLElement).getBoundingClientRect();
                        if (r.width > 0 && r.height > 0) return true;
                    }
                }
                return false;
            }, selectors);
        } catch {
            return null;
        }
    };

const teamsSdkProbe: ScreenShareProbe = async (page: Page) => {
    try {
        return await page.evaluate(() => {
            const fn = (window as any).__lmaScreenShareActive;
            return typeof fn === 'function' ? Boolean(fn()) : null;
        });
    } catch {
        return null;
    }
};

/**
 * The probe for a platform and join method, or null when the platform gives
 * no screen-share signal.
 */
export function screenShareProbeFor(platform: string, teamsMethod?: string): ScreenShareProbe | null {
    switch ((platform || '').toUpperCase()) {
        case 'ZOOM':
            return selectorProbe(SHARE_SELECTORS.ZOOM);
        case 'TEAMS':
            return teamsMethod === 'sdk' ? teamsSdkProbe : selectorProbe(SHARE_SELECTORS.TEAMS);
        default:
            return null;
    }
}
