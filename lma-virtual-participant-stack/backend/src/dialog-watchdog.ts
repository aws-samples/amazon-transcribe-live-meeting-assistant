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
import { Page } from 'rebrowser-puppeteer';
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
    stableChecksRequired?: number;
    manualActionTimeoutSec?: number;
}

/**
 * Start the AI-driven dialog watchdog. Returns a stop function so callers
 * can cancel it on cleanup. Safe to call when the resolver is disabled —
 * it's a no-op in that case.
 */
export function startDialogWatchdog(page: Page, opts: WatchdogOpts): () => void {
    if (!isResolverEnabled()) {
        return () => {};
    }
    const checkIntervalMs = opts.checkIntervalMs ?? 5000;
    const stableChecksRequired = opts.stableChecksRequired ?? 2;
    const manualActionTimeoutSec = opts.manualActionTimeoutSec ?? 180;
    const selectors = [
        ...COMMON_DIALOG_SELECTORS,
        ...(PLATFORM_DIALOG_SELECTORS[opts.platform] || []),
    ];

    let consecutive = 0;
    let lastHandledHtml = '';
    let escalated = false;

    const interval = setInterval(async () => {
        try {
            if (page.isClosed()) {
                clearInterval(interval);
                return;
            }
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
                return;
            }
            const fingerprint = dialogs.map((d) => d.html).join('|');
            if (fingerprint === lastHandledHtml) {
                return;
            }
            consecutive += 1;
            if (consecutive < stableChecksRequired) return;

            const analysis = await analyzeUnknownDialog(page, { platform: opts.platform });
            lastHandledHtml = fingerprint;
            consecutive = 0;
            if (!analysis) return;
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
        } catch (err) {
            console.warn(`[dialog-watchdog ${opts.platform}] error:`, err);
        }
    }, checkIntervalMs);

    return () => clearInterval(interval);
}
