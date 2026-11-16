/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT-0 License.
 * See the LICENSE file in the project root for full license information.
 */

/**
 * Leaving the meeting on the platform before the browser closes (#1127).
 *
 * Closing the browser does not take the Virtual Participant out of the
 * meeting straight away: the platform keeps it on the participant list until
 * its own connection timeout, which on Zoom was several minutes. So whenever
 * the VP ends while it is still in the meeting (stopped from LMA, asked to
 * leave in the chat, left alone, or at its maximum duration) it first uses the
 * platform's own leave control, the way a person would, and only then closes
 * the browser.
 *
 * The leave is given a short, fixed budget. It runs inside the stop grace
 * period (ECS sends SIGKILL 30 seconds after SIGTERM, and a MicroVM's
 * terminate hook has 30 seconds), and everything after it (the recording
 * upload, the profile save) still needs that time. A leave that cannot find
 * its control, or takes too long, is abandoned and the shutdown carries on as
 * before.
 */

import type { ExitInfo } from './details.js';
import { createLogger } from './logger.js';

const log = createLogger('meeting-leave');

/** How long the leave may take before the shutdown carries on without it. */
export const LEAVE_BUDGET_MS = 8_000;

/** How long to wait for a confirmation some clients show after Leave. */
const CONFIRM_WAIT_MS = 3_000;

/** How long to give the client to send its leave once the last click landed. */
const SETTLE_MS = 1_000;

const POLL_MS = 250;

/**
 * One click in a leave: candidate selectors, and optionally the label the
 * control must carry (its aria-label, or its text when it has none).
 */
export interface LeaveStep {
    selectors: readonly string[];
    label?: RegExp;
}

/** The platform's leave: the control that leaves, and the confirmation that may follow. */
export interface LeavePlan {
    platform: string;
    leave: LeaveStep;
    confirm?: LeaveStep;
}

/**
 * Labels never clicked by a leave, whichever selector matched: ending the
 * meeting for everyone (shown beside Leave when the VP's account is the host),
 * and leaving only a breakout room.
 */
export const NEVER_CLICK = /\bend\b|breakout/i;

/**
 * The browser-client leave plans.
 *
 * ASSUMPTION (verify live): the Zoom, Webex and Chime selectors below are the
 * clients' current labels, not ones already read by the join code; the Teams
 * and Meet ones are the hang-up selectors the handlers already watch.
 */
export const LEAVE_PLANS = {
    ZOOM: {
        platform: 'ZOOM',
        // The footer Leave opens a menu; its "Leave Meeting" item leaves.
        leave: {
            selectors: ['button.footer__leave-btn', 'button[aria-label="Leave" i]', 'button[aria-label^="Leave" i]'],
            label: /^leave\b/i,
        },
        confirm: {
            selectors: ['button.leave-meeting-options__btn', '[role="dialog"] button', 'button'],
            label: /^leave meeting$/i,
        },
    },
    TEAMS: {
        platform: 'TEAMS',
        leave: {
            selectors: ['#hangup-button', '[data-tid="hangup-button"]', 'button[aria-label^="Leave" i]', 'button[aria-label*="hang up" i]'],
        },
        confirm: { selectors: ['[role="dialog"] button'], label: /^leave$/i },
    },
    GOOGLE_MEET: {
        platform: 'GOOGLE_MEET',
        leave: {
            selectors: ['button[aria-label="Leave call" i]', 'button[aria-label^="Leave call" i]', '[role="button"][aria-label^="Leave call" i]'],
        },
        confirm: { selectors: ['[role="dialog"] button'], label: /^(just )?leave( the)? call$/i },
    },
    WEBEX: {
        platform: 'WEBEX',
        leave: {
            selectors: ['button[aria-label^="Leave" i]', 'mdc-button[aria-label^="Leave" i]', 'button[title^="Leave" i]', '[data-test*="leave" i]'],
            label: /^leave\b/i,
        },
        confirm: { selectors: ['[role="dialog"] button', 'button', 'mdc-button'], label: /^leave meeting$/i },
    },
    CHIME: {
        platform: 'CHIME',
        leave: {
            selectors: ['button[data-testid="button"][aria-label^="Leave" i]', 'button[aria-label^="Leave" i]'],
            label: /^leave\b/i,
        },
        confirm: { selectors: ['[role="dialog"] button', '[role="menu"] button', 'button'], label: /^leave meeting$/i },
    },
} as const satisfies Record<string, LeavePlan>;

/**
 * Whether the VP is still in the meeting when it ends for this reason, so it
 * should leave on the platform before the browser closes.
 *
 * An exhaustive switch, as in `didJoinMeeting`, so a new reason code is a
 * compile error until it is classified here.
 */
export function shouldLeaveAfterExit(info: ExitInfo): boolean {
    switch (info.reason) {
        case 'end-command':
        case 'consent-objection':
        case 'alone-in-meeting':
        case 'meeting-timeout':
        case 'unknown':
            return true;
        case 'removed-from-meeting':
        case 'host-ended':
        case 'page-closed':
        case 'never-joined':
            return false;
        default: {
            const unclassified: never = info.reason;
            return unclassified;
        }
    }
}

// The parts of Playwright's Page, Frame and Locator a leave uses, so the
// tests can drive it without a browser.
export interface LeaveLocator {
    count(): Promise<number>;
    nth(index: number): LeaveLocator;
    isVisible(): Promise<boolean>;
    getAttribute(name: string): Promise<string | null>;
    innerText(options?: { timeout?: number }): Promise<string>;
    click(options?: { timeout?: number }): Promise<void>;
}
export interface LeaveFrame {
    locator(selector: string): LeaveLocator;
}
export interface LeavePage {
    isClosed(): boolean;
    frames(): LeaveFrame[];
}

const MAX_CANDIDATES_PER_SELECTOR = 10;

async function controlLabel(control: LeaveLocator): Promise<string> {
    const aria = (await control.getAttribute('aria-label').catch(() => null))?.trim();
    if (aria) return aria;
    const title = (await control.getAttribute('title').catch(() => null))?.trim();
    if (title) return title;
    return (await control.innerText({ timeout: 500 }).catch(() => '')).trim();
}

/**
 * Click the first visible control the step describes, in any frame (Webex
 * runs in an iframe). Returns the label clicked, or null when none is on the
 * page.
 */
export async function clickLeaveStep(page: LeavePage, step: LeaveStep): Promise<string | null> {
    for (const selector of step.selectors) {
        for (const frame of page.frames()) {
            let matches: LeaveLocator;
            let count: number;
            try {
                matches = frame.locator(selector);
                count = Math.min(await matches.count(), MAX_CANDIDATES_PER_SELECTOR);
            } catch {
                continue;
            }
            for (let i = 0; i < count; i += 1) {
                const control = matches.nth(i);
                try {
                    if (!(await control.isVisible())) continue;
                    const label = await controlLabel(control);
                    if (NEVER_CLICK.test(label)) continue;
                    if (step.label && !step.label.test(label)) continue;
                    await control.click({ timeout: 2_000 });
                    return label || selector;
                } catch {
                    // Detached or covered: try the next candidate.
                }
            }
        }
    }
    return null;
}

const sleep = (ms: number): Promise<void> => new Promise((resolve) => setTimeout(resolve, ms));

/**
 * Leave the meeting through the platform's own controls: click Leave, then the
 * confirmation when the client shows one. Resolves whether or not a control
 * was found; never throws.
 */
export async function leaveByClicking(page: LeavePage, plan: LeavePlan): Promise<boolean> {
    if (page.isClosed()) return false;
    const left = await clickLeaveStep(page, plan.leave);
    if (!left) {
        log.warn(`[leave] ${plan.platform}: no leave control on the page`);
        return false;
    }
    log.info(`[leave] ${plan.platform}: clicked "${left}"`);
    if (plan.confirm) {
        const deadline = Date.now() + CONFIRM_WAIT_MS;
        while (Date.now() < deadline && !page.isClosed()) {
            const confirmed = await clickLeaveStep(page, plan.confirm);
            if (confirmed) {
                log.info(`[leave] ${plan.platform}: confirmed with "${confirmed}"`);
                break;
            }
            await sleep(POLL_MS);
        }
    }
    await sleep(SETTLE_MS);
    return true;
}

export type LeaveOutcome = 'left' | 'not-left' | 'timeout' | 'failed';

/**
 * Run a handler's leave within `budgetMs`. Never throws and never takes longer
 * than the budget, so a stuck page cannot hold up the rest of the shutdown.
 */
export async function leaveWithinBudget(
    leave: () => Promise<boolean | void>,
    budgetMs: number = LEAVE_BUDGET_MS,
): Promise<LeaveOutcome> {
    let timer: NodeJS.Timeout | undefined;
    const timeout = new Promise<LeaveOutcome>((resolve) => {
        timer = setTimeout(() => resolve('timeout'), budgetMs);
    });
    const attempt = (async (): Promise<LeaveOutcome> => {
        try {
            const result = await leave();
            return result === false ? 'not-left' : 'left';
        } catch (error) {
            log.warn(`[leave] failed: ${error}`);
            return 'failed';
        }
    })();
    try {
        return await Promise.race([attempt, timeout]);
    } finally {
        if (timer) clearTimeout(timer);
    }
}
