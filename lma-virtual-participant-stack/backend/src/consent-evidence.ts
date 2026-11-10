/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT-0 License.
 * See the LICENSE file in the project root for full license information.
 */

/**
 * Screenshots kept as consent evidence: the meeting chat, with the notice and
 * the replies, and the participant list, taken when consent is granted and on
 * every objection.
 *
 * Taken from the meeting page with Playwright rather than from the X11 display
 * the key frames are grabbed from (keyframe-sampler.ts), so the image is the
 * meeting and nothing a live-view viewer has open over it. Stored as the key
 * frames are, in the recordings bucket, under the folder the meeting consent
 * resolver accepts: lma-consent-evidence/<CallId, posixified>/ (see
 * docs/meeting-consent.md), with the same posixify rule.
 *
 * Opening the participant list can hide the chat on platforms whose side panel
 * shows one at a time, so a handler that opens it also says how to put the
 * chat back; every step is best effort, and a failure only means fewer images.
 */
import type { Frame, Page } from 'playwright-core';
import { randomBytes } from 'crypto';
import { posixifyFilename } from './keyframe-sampler.js';
import { createLogger } from './logger.js';

/** Where a panel's buttons are: the page, or the frame the meeting client runs in (Webex). */
type PanelRoot = Page | Frame;

const log = createLogger('consent-evidence');

// The same variable, and default, as the meeting consent resolver's, which refuses evidence outside it.
export const CONSENT_EVIDENCE_PREFIX = process.env.CONSENT_EVIDENCE_PREFIX || 'lma-consent-evidence/';

/** What one screenshot shows. */
export type EvidenceView = 'chat' | 'participants' | 'meeting';

export interface EvidenceShot {
    view: EvidenceView;
    image: Buffer;
    capturedAt: string;
}

/** How a handler shows its participant list for a screenshot and then restores the chat. */
export interface EvidencePanels {
    /** Shows the participant list; true when it is on screen. */
    showParticipants?: () => Promise<boolean>;
    /** Puts the page back as the VP uses it, with the chat open. */
    restore?: () => Promise<void>;
    /** Whether the chat and the participant list are on screen together already (Chime). */
    bothVisible?: boolean;
}

/** The S3 key of one evidence image. Sorts in time order; unique per image. */
export function consentEvidenceKey(callId: string, capturedAtMs: number, view: EvidenceView, suffix = randomBytes(3).toString('hex')): string {
    const stamp = new Date(capturedAtMs).toISOString().replace(/[:.]/g, '-');
    return `${CONSENT_EVIDENCE_PREFIX}${posixifyFilename(callId)}/${stamp}-${view}-${suffix}.jpg`;
}

async function shoot(page: Page, view: EvidenceView): Promise<EvidenceShot | null> {
    try {
        const image = await page.screenshot({ type: 'jpeg', quality: 70, timeout: 10_000 });
        return { view, image, capturedAt: new Date().toISOString() };
    } catch (error) {
        log.warn(`[consent] could not take the ${view} screenshot: ${String(error)}`);
        return null;
    }
}

/**
 * The screenshots for one moment: the chat as the VP has it open, then the
 * participant list, then the chat restored. Never throws.
 */
export async function captureMeetingPanels(page: Page, panels: EvidencePanels = {}): Promise<EvidenceShot[]> {
    const shots: EvidenceShot[] = [];
    if (page.isClosed()) return shots;
    const first = await shoot(page, panels.bothVisible ? 'meeting' : 'chat');
    if (first) shots.push(first);
    if (panels.bothVisible || !panels.showParticipants) return shots;
    let shown = false;
    try {
        shown = await panels.showParticipants();
    } catch (error) {
        log.warn(`[consent] could not show the participant list: ${String(error)}`);
    }
    if (shown) {
        await new Promise((resolve) => setTimeout(resolve, 800));
        const list = await shoot(page, 'participants');
        if (list) shots.push(list);
    }
    try {
        await panels.restore?.();
    } catch (error) {
        log.warn(`[consent] could not restore the chat panel: ${String(error)}`);
    }
    return shots;
}

/**
 * Clicks the first element matching one of `selectors` that is visible, and
 * reports whether one was. For opening and closing side panels by their
 * toolbar buttons, without the AI resolver's retries: evidence is not worth
 * holding the chat up for.
 */
export async function clickFirstVisible(page: PanelRoot, selectors: string[]): Promise<boolean> {
    for (const selector of selectors) {
        try {
            const handle = await page.$(selector);
            if (handle && (await handle.isVisible())) {
                await handle.click({ timeout: 3000 });
                return true;
            }
        } catch {
            /* try the next selector */
        }
    }
    return false;
}

/** Whether any of `selectors` matches a visible element. */
export async function anyVisible(page: PanelRoot, selectors: string[]): Promise<boolean> {
    for (const selector of selectors) {
        try {
            const handle = await page.$(selector);
            if (handle && (await handle.isVisible())) return true;
        } catch {
            /* try the next selector */
        }
    }
    return false;
}

/**
 * Panels for a browser join whose participant list opens from a toolbar
 * button: open it (unless it is already open), and afterwards close it again
 * if it was opened here, then let the handler reopen its chat.
 */
export function toolbarPanels(
    page: PanelRoot,
    options: { listVisible: string[]; open: string[]; close: string[]; reopenChat?: () => Promise<unknown> },
): EvidencePanels {
    let openedHere = false;
    return {
        showParticipants: async () => {
            if (await anyVisible(page, options.listVisible)) return true;
            openedHere = await clickFirstVisible(page, options.open);
            if (!openedHere) return false;
            for (let i = 0; i < 10; i += 1) {
                if (await anyVisible(page, options.listVisible)) return true;
                await new Promise((resolve) => setTimeout(resolve, 300));
            }
            // Clicked, but no list appeared: no screenshot is labelled as one.
            // restore() still closes what the click may have opened.
            return false;
        },
        restore: async () => {
            if (openedHere) await clickFirstVisible(page, options.close);
            openedHere = false;
            if (options.reopenChat) await options.reopenChat();
        },
    };
}
