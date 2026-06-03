/* eslint-disable @typescript-eslint/no-explicit-any */
/**
 * Background AI-driven watchdog that catches unknown modal dialogs across
 * the meeting lifecycle (sign-in / pre-join / waiting-room / in-meeting).
 *
 * Every ~5s it counts visible modals on the page. When the same modal stays
 * visible across two consecutive checks (i.e. our hardcoded auto-dismiss
 * handlers haven't dealt with it), it asks Claude (via vision + DOM
 * snapshot) to classify the dialog. The action depends on the type:
 *
 *   - CONSENT / RECORDING_NOTICE  → click the primary action (auto-dismiss)
 *   - CAPTCHA / LOGIN_REQUIRED / SSO_REDIRECT / BLOCKED → escalate via
 *                                  setManualActionRequired so the React UI
 *                                  shows the VNC takeover banner.
 *   - OTHER                       → leave alone (we don't auto-click
 *                                  anything we can't classify).
 *
 * Used by every platform handler (Zoom, Teams, Webex, Chime). The
 * platform-specific dialog selectors are passed in so we cover platform-
 * specific modal containers (`.zm-modal` for Zoom, `[data-tid="modal"]`
 * for Teams, etc.).
 */
import { Page } from 'playwright-core';
import {
    analyzeUnknownDialog,
    isResolverEnabled,
    scrollIntoViewAndClick,
} from './ai-dom-resolver.js';
import { details } from './details.js';

const COMMON_DIALOG_SELECTORS = [
    '[role="dialog"]',
    '[role="alertdialog"]',
    '.ReactModal__Content',
];
const PLATFORM_DIALOG_SELECTORS: Record<string, string[]> = {
    ZOOM: ['.zm-modal', '.zm-modal-legacy'],
    TEAMS: ['[data-tid="modal"]', '.fui-Dialog__surface'],
    WEBEX: ['.md-modal', '[role="dialog"][aria-modal="true"]'],
    CHIME: ['[data-test-id*="modal"]'],
};

const AUTOCLICK_TYPES = new Set(['CONSENT', 'RECORDING_NOTICE']);

interface WatchdogOpts {
    platform: 'ZOOM' | 'TEAMS' | 'WEBEX' | 'CHIME';
    checkIntervalMs?: number;
    /** Slow cadence used once the page has been quiet (no new dialogs) for a
     *  while. Defaults to 4× the fast interval. The watchdog snaps back to the
     *  fast interval the moment a new dialog appears. */
    idleCheckIntervalMs?: number;
    /** Consecutive quiet checks before backing off to the slow cadence. */
    quietChecksBeforeBackoff?: number;
    stableChecksRequired?: number;
    manualActionTimeoutSec?: number;
}

/**
 * Start the AI-driven dialog watchdog. Returns a stop function so callers
 * can cancel it on cleanup. Safe to call when the resolver is disabled —
 * it's a no-op in that case.
 *
 * Cadence is adaptive: the DOM scan runs at the fast interval (default 5s)
 * while dialogs are appearing or just after start, then backs off to the
 * idle interval (default 20s) once the page has been quiet for several
 * checks — most of a meeting. It snaps back to fast the instant a new modal
 * shows up. This keeps CAPTCHA/consent detection prompt while cutting the
 * steady-state DOM-scan rate (and its CPU) ~4× during a normal meeting.
 */
export function startDialogWatchdog(page: Page, opts: WatchdogOpts): () => void {
    if (!isResolverEnabled()) {
        return () => {};
    }
    const checkIntervalMs = opts.checkIntervalMs ?? 5000;
    const idleCheckIntervalMs = opts.idleCheckIntervalMs ?? checkIntervalMs * 4;
    const quietChecksBeforeBackoff = opts.quietChecksBeforeBackoff ?? 3;
    const stableChecksRequired = opts.stableChecksRequired ?? 2;
    const manualActionTimeoutSec = opts.manualActionTimeoutSec ?? 180;
    const selectors = [
        ...COMMON_DIALOG_SELECTORS,
        ...(PLATFORM_DIALOG_SELECTORS[opts.platform] || []),
    ];

    let consecutive = 0;
    let lastHandledHtml = '';
    let escalated = false;
    let quietChecks = 0;
    let stopped = false;
    let timer: NodeJS.Timeout | undefined;

    // One scan iteration. Returns true if a dialog was present this tick
    // (used to drive the adaptive cadence: any dialog → stay/return to fast).
    const tick = async (): Promise<boolean> => {
        const dialogs = await page.evaluate((sels: string[]) => {
            const set = new Set<Element>();
            for (const s of sels) document.querySelectorAll(s).forEach((e) => set.add(e));
            const visible: { html: string }[] = [];
            for (const el of Array.from(set)) {
                const r = (el as HTMLElement).getBoundingClientRect();
                if (r.width === 0 || r.height === 0) continue;
                visible.push({ html: ((el as HTMLElement).outerHTML || '').slice(0, 200) });
            }
            return visible;
        }, selectors);

        if (dialogs.length === 0) {
            consecutive = 0;
            if (escalated) {
                escalated = false;
                if (details.invite.virtualParticipantId) {
                    const { createStatusManager } = await import('./status-manager.js');
                    await createStatusManager(details.invite.virtualParticipantId).clearManualAction();
                }
            }
            return false;
        }
        const fingerprint = dialogs.map((d) => d.html).join('|');
        if (fingerprint === lastHandledHtml) {
            return true;
        }
        consecutive += 1;
        if (consecutive < stableChecksRequired) return true;

        const analysis = await analyzeUnknownDialog(page, { platform: opts.platform });
        lastHandledHtml = fingerprint;
        consecutive = 0;
        if (!analysis) return true;
        console.log(`[dialog-watchdog ${opts.platform}] ${analysis.type}: ${analysis.message}`);

        if (
            !analysis.needsHuman &&
            analysis.primaryActionSelector &&
            AUTOCLICK_TYPES.has(analysis.type)
        ) {
            const ok = await scrollIntoViewAndClick(page, analysis.primaryActionSelector);
            if (ok) {
                console.log(`[dialog-watchdog ${opts.platform}] auto-dismissed via ${analysis.primaryActionSelector}`);
            } else {
                console.warn(`[dialog-watchdog ${opts.platform}] click failed on ${analysis.primaryActionSelector}`);
            }
        } else if (analysis.needsHuman && details.invite.virtualParticipantId) {
            const { createStatusManager } = await import('./status-manager.js');
            await createStatusManager(details.invite.virtualParticipantId).setManualActionRequired(
                analysis.type,
                analysis.message || `Manual action required in the ${opts.platform} web client.`,
                manualActionTimeoutSec,
            );
            escalated = true;
        }
        return true;
    };

    const loop = async (): Promise<void> => {
        if (stopped || page.isClosed()) return;
        let hadDialog = false;
        try {
            hadDialog = await tick();
        } catch (err) {
            console.warn(`[dialog-watchdog ${opts.platform}] error:`, err);
        }
        // Adaptive cadence: any dialog this tick resets to fast; otherwise
        // count quiet ticks and back off to the idle interval once we've been
        // quiet long enough. This keeps detection prompt when something pops
        // up but cuts the steady-state scan rate during a normal meeting.
        if (hadDialog) {
            quietChecks = 0;
        } else if (quietChecks < quietChecksBeforeBackoff) {
            quietChecks += 1;
        }
        const nextDelay =
            quietChecks >= quietChecksBeforeBackoff ? idleCheckIntervalMs : checkIntervalMs;
        if (!stopped && !page.isClosed()) {
            timer = setTimeout(loop, nextDelay);
        }
    };

    timer = setTimeout(loop, checkIntervalMs);

    return () => {
        stopped = true;
        if (timer) clearTimeout(timer);
    };
}
